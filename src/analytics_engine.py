import duckdb
import pandas as pd

class AnalyticsEngine:
    def __init__(self, db_path="solar_om.duckdb"):
        self.conn = duckdb.connect(db_path)
        
    def get_events(self):
        """Fetch all error events for the UI selector."""
        query = "SELECT event_id, inverter_id, start_time, end_time, error_code, description FROM error_events"
        return self.conn.execute(query).df()
        
    def get_event_details(self, event_id):
        """Get full details of a specific event, including linked tickets."""
        query = f"""
            SELECT e.*, t.ticket_id, t.status, t.issue_category as ticket_category
            FROM error_events e
            LEFT JOIN service_tickets t ON e.inverter_id = t.inverter_id
                AND t.create_time <= e.end_time
                AND (t.end_time IS NULL OR t.end_time >= e.start_time)
            WHERE e.event_id = '{event_id}'
        """
        df = self.conn.execute(query).df()
        return df.iloc[0].to_dict() if not df.empty else {}

    def get_plant_overview(self):
        """
        Return KPI summary for the most recent day in the dataset:
        latest_date, total_generation_kwh, online_inverters, total_inverters,
        active_alarm_count, estimated_loss_kwh, and an hourly generation DataFrame.
        """
        latest_date = self.conn.execute(
            "SELECT MAX(DATE(timestamp)) FROM telemetry_minute"
        ).fetchone()[0]

        day_str = str(latest_date)

        kpi = self.conn.execute(f"""
            SELECT
                ROUND(SUM(active_power_kw) / 12.0, 1)   AS total_kwh,
                COUNT(DISTINCT inverter_id)               AS total_inv
            FROM telemetry_minute
            WHERE DATE(timestamp) = '{day_str}'
        """).fetchone()
        total_kwh, total_inv = kpi

        online_inv = self.conn.execute(f"""
            SELECT COUNT(DISTINCT inverter_id)
            FROM telemetry_minute
            WHERE DATE(timestamp) = '{day_str}'
              AND active_power_kw > 0
        """).fetchone()[0]

        active_alarms = self.conn.execute(f"""
            SELECT COUNT(*)
            FROM error_events
            WHERE start_time <= '{day_str} 23:59:59'
              AND end_time   >= '{day_str} 00:00:00'
        """).fetchone()[0]

        # Estimate daily loss: for each active event, compare target vs peer avg
        est_loss_kwh = self.conn.execute(f"""
            WITH events_today AS (
                SELECT event_id, inverter_id, start_time, end_time
                FROM error_events
                WHERE start_time <= '{day_str} 23:59:59'
                  AND end_time   >= '{day_str} 00:00:00'
            ),
            peer_avg AS (
                SELECT timestamp, AVG(active_power_kw) AS baseline
                FROM telemetry_minute
                GROUP BY timestamp
            )
            SELECT COALESCE(
                ROUND(SUM(GREATEST(0, p.baseline - t.active_power_kw)) / 12.0, 1),
                0
            ) AS loss_kwh
            FROM events_today e
            JOIN telemetry_minute t
                ON t.inverter_id = e.inverter_id
               AND t.timestamp BETWEEN e.start_time AND e.end_time
            JOIN peer_avg p ON p.timestamp = t.timestamp
        """).fetchone()[0]

        hourly_df = self.conn.execute(f"""
            SELECT DATE_TRUNC('hour', timestamp) AS hour,
                   ROUND(SUM(active_power_kw) / 12.0, 1) AS kwh
            FROM telemetry_minute
            WHERE DATE(timestamp) = '{day_str}'
            GROUP BY 1
            ORDER BY 1
        """).df()

        return {
            "latest_date":    latest_date,
            "total_kwh":      total_kwh or 0.0,
            "total_inv":      total_inv or 0,
            "online_inv":     online_inv or 0,
            "active_alarms":  active_alarms or 0,
            "est_loss_kwh":   est_loss_kwh or 0.0,
            "hourly_df":      hourly_df,
        }

    def calculate_impact(self, inverter_id, start_time, end_time):
        """
        Calculate energy loss by comparing target inverter against peers.
        Returns total loss in kWh and the time-series dataframe for plotting.
        """
        # We add 30 mins padding before and after for visualization context
        query = f"""
            WITH peer_avg AS (
                SELECT timestamp, AVG(active_power_kw) as baseline_power
                FROM telemetry_minute
                WHERE inverter_id != '{inverter_id}'
                GROUP BY timestamp
            ),
            target_data AS (
                SELECT timestamp, active_power_kw
                FROM telemetry_minute
                WHERE inverter_id = '{inverter_id}'
            )
            SELECT
                t.timestamp,
                t.active_power_kw as actual_power,
                p.baseline_power,
                CASE
                    WHEN t.timestamp >= '{start_time}' AND t.timestamp <= '{end_time}'
                    THEN GREATEST(0, p.baseline_power - t.active_power_kw)
                    ELSE 0
                END as power_loss_kw
            FROM target_data t
            JOIN peer_avg p ON t.timestamp = p.timestamp
            JOIN solar_altitude a ON a.timestamp = t.timestamp AND a.altitude >= 0
            WHERE t.timestamp BETWEEN CAST('{start_time}' AS TIMESTAMP) - INTERVAL 30 MINUTE
                                  AND CAST('{end_time}' AS TIMESTAMP) + INTERVAL 30 MINUTE
            ORDER BY t.timestamp
        """
        df = self.conn.execute(query).df()
        
        # Data is 5-min resolution: each row = 5/60 h → sum(kW) / 12 = kWh
        total_loss_kwh = df['power_loss_kw'].sum() / 12.0
        
        return total_loss_kwh, df
