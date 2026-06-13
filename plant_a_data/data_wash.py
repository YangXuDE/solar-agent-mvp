import pandas as pd

df = pd.read_csv(
    'main_monitoring_data.csv',
    encoding='utf-8-sig',
    sep=';',
    decimal=',',
    index_col=0,
    low_memory=False
)

cols_to_check = df.columns[3:10]
df_cleaned = df.dropna(subset=cols_to_check)

df_cleaned.to_csv('main_monitoring_data_washed.csv', sep=';', decimal=',', encoding='utf-8-sig')

print(f"数据清理完成！原始行数: {len(df)}，清理后行数: {len(df_cleaned)}")