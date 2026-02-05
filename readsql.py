import sqlite3
import pandas as pd

# 连接数据库
conn = sqlite3.connect('photosnas.db')

try:
    # 查询所有表名（可选）
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print("数据库中的表:", [t[0] for t in tables])

    # 读取目标表数据到DataFrame
    df = pd.read_sql_query("SELECT * FROM photo_scores", conn)
    
    # 导出到Excel文件
    excel_filename = "photo_scores_export.xlsx"
    df.to_excel(excel_filename, index=False, engine='openpyxl')
    
    print(f"成功导出 {len(df)} 条记录到 {excel_filename}")

except Exception as e:
    print(f"操作出错: {str(e)}")

finally:
    # 关闭连接
    conn.close()