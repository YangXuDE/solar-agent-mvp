import duckdb
import os
import pandas as pd


def init_duckdb(db_path="solar_om.duckdb", data_dir="plant_a_data"):
    conn = duckdb.connect(db_path)

    # Skip rebuild if real data is already loaded
    try:
        sample = conn.execute("SELECT inverter_id FROM telemetry_minute LIMIT 1").fetchone()
        if sample and "INV 01." in str(sample[0]):
            return conn
    except Exception:
        pass

    print("Building database from real plant data...")
    conn.execute("DROP TABLE IF EXISTS telemetry_minute")
    conn.execute("DROP TABLE IF EXISTS error_events")
    conn.execute("DROP TABLE IF EXISTS service_tickets")
    conn.execute("DROP TABLE IF EXISTS solar_altitude")

    _build_solar_altitude(conn, data_dir)
    _build_telemetry(conn, data_dir)
    _build_error_events(conn, data_dir)
    _build_service_tickets(conn, data_dir)
    _filter_plant_wide_events(conn)

    print("Database ready.")
    return conn


def _load_monitoring_data(data_dir):
    """Load main_monitoring_data — CSV preferred, falls back to parquet.
    Rows where ALL inverter U_DC values are null are dropped immediately.
    """
    washed_path = os.path.join(data_dir, "main_monitoring_data_washed.csv")
    csv_path = os.path.join(data_dir, "main_monitoring_data.csv")
    parquet_path = os.path.join(data_dir, "main_monitoring_data.parquet")

    if os.path.exists(washed_path):
        print("  Source: main_monitoring_data_washed.csv")
        df = pd.read_csv(
            washed_path,
            sep=";",
            decimal=",",
            index_col=0,
            encoding="utf-8-sig",
        )
    elif os.path.exists(csv_path):
        print("  Source: main_monitoring_data.csv")
        df = pd.read_csv(
            csv_path,
            sep=";",
            decimal=",",
            index_col=0,
            encoding="utf-8-sig",
        )
    elif os.path.exists(parquet_path):
        print("  Source: main_monitoring_data.parquet")
        df = pd.read_parquet(parquet_path)
    else:
        raise FileNotFoundError(
            f"Neither main_monitoring_data.csv nor .parquet found in {data_dir}"
        )

    # Drop rows where ALL inverter U_DC values are null (monitoring system offline)
    udc_cols = [c for c in df.columns if "/ U_DC (V)" in c]
    before = len(df)
    df = df[df[udc_cols].notna().any(axis=1)]
    print(f"  U_DC filter: kept {len(df):,} / {before:,} rows (dropped {before - len(df):,})")
    return df


def _build_solar_altitude(conn, data_dir):
    """Store Plant / Altitude (°) per timestamp — used to exclude nighttime from charts."""
    alt_col = "Plant / Altitude (°)"
    washed_path = os.path.join(data_dir, "main_monitoring_data_washed.csv")
    csv_path = os.path.join(data_dir, "main_monitoring_data.csv")
    parquet_path = os.path.join(data_dir, "main_monitoring_data.parquet")

    if os.path.exists(washed_path):
        df = pd.read_csv(
            washed_path,
            sep=";",
            decimal=",",
            index_col=0,
            encoding="utf-8-sig",
            usecols=lambda c: c in ("timestamp", alt_col),
        )
    elif os.path.exists(csv_path):
        df = pd.read_csv(
            csv_path,
            sep=";",
            decimal=",",
            index_col=0,
            encoding="utf-8-sig",
            usecols=lambda c: c in ("timestamp", alt_col),
        )
    else:
        df = pd.read_parquet(parquet_path, columns=[alt_col])

    df = df[[alt_col]].dropna().reset_index()
    df.columns = ["timestamp", "altitude"]
    df["timestamp"] = (
        pd.to_datetime(df["timestamp"], format="%Y.%m.%d %H:%M", utc=True)
        .dt.tz_convert("Europe/Berlin")
        .dt.tz_localize(None)
    )
    conn.execute("CREATE TABLE solar_altitude AS SELECT * FROM df")
    print(f"  Solar altitude: {len(df):,} rows.")


def _build_telemetry(conn, data_dir):
    print("Loading telemetry (this may take ~30s)...")
    df = _load_monitoring_data(data_dir)
    power_cols = [c for c in df.columns if "/ P_AC (kW)" in c]

    conn.execute("""
        CREATE TABLE telemetry_minute (
            timestamp TIMESTAMP,
            inverter_id VARCHAR,
            active_power_kw DOUBLE
        )
    """)

    chunk_size = 50000
    for i in range(0, len(df), chunk_size):
        chunk = df[power_cols].iloc[i : i + chunk_size].reset_index()
        chunk_long = chunk.melt(
            id_vars="timestamp", var_name="inv_col", value_name="active_power_kw"
        )
        chunk_long = chunk_long.dropna(subset=["active_power_kw"])
        chunk_long["inverter_id"] = chunk_long["inv_col"].str.replace(
            " / P_AC (kW)", "", regex=False
        )
        # Parquet timestamps are UTC; convert to Europe/Berlin local time
        chunk_long["timestamp"] = (
            pd.to_datetime(chunk_long["timestamp"], format="%Y.%m.%d %H:%M", utc=True)
            .dt.tz_convert("Europe/Berlin")
            .dt.tz_localize(None)
        )
        chunk_long = chunk_long[["timestamp", "inverter_id", "active_power_kw"]]
        conn.execute("INSERT INTO telemetry_minute SELECT * FROM chunk_long")

    row_count = conn.execute("SELECT COUNT(*) FROM telemetry_minute").fetchone()[0]
    print(f"Telemetry loaded: {row_count:,} rows.")


def _build_error_events(conn, data_dir):
    print("Processing error events...")
    df_mon = _load_monitoring_data(data_dir)
    udc_cols = [c for c in df_mon.columns if "/ U_DC (V)" in c]
    # Build set of valid UTC string timestamps (same index format as errorcodes parquet)
    valid_utc_index = set(df_mon.index[df_mon[udc_cols].notna().any(axis=1)])
    del df_mon

    df_err = pd.read_parquet(os.path.join(data_dir, "errorcodes.parquet"))
    # Filter by valid timestamps BEFORE timezone conversion (both share same UTC string index)
    df_err = df_err[df_err.index.isin(valid_utc_index)]
    # Parquet timestamps are UTC; convert to Europe/Berlin local time
    df_err.index = (
        pd.to_datetime(df_err.index, format="%Y.%m.%d %H:%M", utc=True)
        .tz_convert("Europe/Berlin")
        .tz_localize(None)
    )
    df_err = df_err.sort_index()

    df_desc = pd.read_excel(
        os.path.join(data_dir, "errorcodes description (important).xlsx")
    )
    code_map = {
        float(row["Dezimal"]): str(row["Code"]) for _, row in df_desc.iterrows()
    }

    events = []
    error_cols = [c for c in df_err.columns if c.endswith("/ Error")]

    for col in error_cols:
        inv_id = col.replace(" / Error", "")
        series = df_err[col].fillna(0)

        # Detect consecutive runs of same error code
        code_change = series != series.shift(1)
        run_id = code_change.cumsum()

        active_mask = series != 0
        if not active_mask.any():
            continue

        active_series = series[active_mask]
        active_run_ids = run_id[active_mask]

        for rid, group in active_series.groupby(active_run_ids):
            # Only keep events >= 15 minutes (3 x 5-min intervals)
            if len(group) < 3:
                continue
            code_val = float(group.iloc[0])
            hex_code = f"{int(code_val):07X}"
            events.append(
                {
                    "event_id": f"E-{len(events) + 1:05d}",
                    "inverter_id": inv_id,
                    "start_time": group.index[0],
                    "end_time": group.index[-1],
                    "error_code": hex_code,
                    "description": code_map.get(
                        code_val, f"Error code {hex_code}"
                    ),
                }
            )

    df_events = pd.DataFrame(events)
    conn.execute("CREATE TABLE error_events AS SELECT * FROM df_events")
    print(f"Created {len(events)} error events.")


def _build_service_tickets(conn, data_dir):
    print("Loading service tickets...")
    df = pd.read_excel(os.path.join(data_dir, "Tickets.xlsx"))

    tickets = []
    for i, row in df.iterrows():
        comp = str(row["component"])
        if pd.isna(row["startdate"]):
            continue

        # Tickets already carry timezone offset (e.g. +02:00); strip to get Berlin local time
        start = pd.to_datetime(row["startdate"]).tz_convert("Europe/Berlin").tz_localize(None)
        end = (
            pd.to_datetime(row["enddate"]).tz_convert("Europe/Berlin").tz_localize(None)
            if not pd.isna(row["enddate"])
            else None
        )
        status = "Open" if end is None else "Closed"
        cat = str(row["category"]) if not pd.isna(row["category"]) else "Unknown"

        tickets.append(
            {
                "ticket_id": f"T-{i + 1:04d}",
                "inverter_id": comp,
                "create_time": start,
                "end_time": end,
                "issue_category": cat,
                "status": status,
            }
        )

    df_tickets = pd.DataFrame(tickets)
    conn.execute("CREATE TABLE service_tickets AS SELECT * FROM df_tickets")
    print(f"Loaded {len(tickets)} service tickets.")


def _filter_plant_wide_events(conn):
    """Remove events where: peers also at 0 (grid outage), or either side has no telemetry at all."""
    print("Filtering plant-wide outage and no-data events...")
    before = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]

    # Remove events where peers were also generating nothing (grid/weather outage)
    conn.execute("""
        DELETE FROM error_events
        WHERE event_id IN (
            SELECT e.event_id
            FROM error_events e
            JOIN (
                SELECT e2.event_id,
                       AVG(CASE WHEN t.active_power_kw > 0 THEN t.active_power_kw END) AS peer_daytime_avg
                FROM error_events e2
                JOIN telemetry_minute t
                    ON t.inverter_id != e2.inverter_id
                    AND t.timestamp BETWEEN e2.start_time AND e2.end_time
                GROUP BY e2.event_id
            ) p ON e.event_id = p.event_id
            WHERE p.peer_daytime_avg IS NULL OR p.peer_daytime_avg < 1.0
        )
    """)

    # Remove events where the target inverter has no telemetry in the window
    conn.execute("""
        DELETE FROM error_events
        WHERE event_id IN (
            SELECT e.event_id
            FROM error_events e
            WHERE NOT EXISTS (
                SELECT 1 FROM telemetry_minute t
                WHERE t.inverter_id = e.inverter_id
                  AND t.timestamp BETWEEN e.start_time AND e.end_time
            )
        )
    """)

    after = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
    print(f"Filtered {before - after} events → {after} events with valid data remain.")
