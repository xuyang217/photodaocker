import sqlite3
import os
import shutil
from pathlib import Path
from datetime import datetime


def convert_photo_paths(source_db, target_db, table_name="photo_scores", path_column="path"):
    """
    读取SQLite数据库，替换路径中的反斜杠，并生成新的数据库文件
    
    参数:
        source_db: 源数据库文件路径
        target_db: 目标数据库文件路径
        table_name: 要处理的表名
        path_column: 包含路径的列名
    """
    
    # 检查源数据库文件是否存在
    if not os.path.exists(source_db):
        print(f"错误: 源数据库文件 '{source_db}' 不存在")
        return False
    
    # 如果目标数据库已存在，先备份
    if os.path.exists(target_db):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"{target_db}.backup_{timestamp}"
        shutil.copy2(target_db, backup_file)
        print(f"已备份现有文件到: {backup_file}")
    
    try:
        # 连接源数据库
        print(f"正在连接源数据库: {source_db}")
        source_conn = sqlite3.connect(source_db)
        source_cursor = source_conn.cursor()
        
        # 检查表是否存在
        source_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        if not source_cursor.fetchone():
            print(f"错误: 表 '{table_name}' 在数据库中不存在")
            source_conn.close()
            return False
        
        # 获取表结构
        print("获取表结构...")
        source_cursor.execute(f"PRAGMA table_info({table_name})")
        columns_info = source_cursor.fetchall()
        columns = [col[1] for col in columns_info]  # 列名在第2个位置
        
        # 检查路径列是否存在
        if path_column not in columns:
            print(f"错误: 列 '{path_column}' 在表 '{table_name}' 中不存在")
            print(f"可用的列: {', '.join(columns)}")
            source_conn.close()
            return False
        
        # 获取原始数据
        print(f"读取表 '{table_name}' 的数据...")
        source_cursor.execute(f"SELECT * FROM {table_name}")
        rows = source_cursor.fetchall()
        column_names = [desc[0] for desc in source_cursor.description]
        
        # 获取路径列的索引
        path_index = column_names.index(path_column)
        
        print(f"找到 {len(rows)} 条记录")
        
        # 连接目标数据库
        print(f"创建目标数据库: {target_db}")
        target_conn = sqlite3.connect(target_db)
        target_cursor = target_conn.cursor()
        
        # 在目标数据库创建相同的表结构
        source_cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
        create_table_sql = source_cursor.fetchone()[0]
        target_cursor.execute(create_table_sql)
        
        # 复制索引
        source_cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table_name,))
        index_sqls = source_cursor.fetchall()
        for index_sql in index_sqls:
            if index_sql[0]:
                target_cursor.execute(index_sql[0])
        
        # 创建更新后的数据
        updated_rows = []
        conversion_stats = {
            'total': len(rows),
            'converted': 0,
            'unchanged': 0
        }
        
        print("处理路径转换...")
        for row in rows:
            row_list = list(row)
            original_path = row_list[path_index]
            
            if original_path and isinstance(original_path, str):
                # 先替换 \feiniu 为 /vol2
                # 注意：在Python字符串中，\f 是换页符，所以需要正确转义
                temp_path = original_path.replace(r'\\feiniu', '/vol2/1000')
                # 然后将所有反斜杠替换为正斜杠
                new_path = temp_path.replace('\\', '/')
                
                row_list[path_index] = new_path
                conversion_stats['converted'] += 1
                
                # 输出转换示例（前5条）
                if conversion_stats['converted'] <= 5:
                    print(f"  示例转换 {conversion_stats['converted']}:")
                    print(f"    原始: {original_path}")
                    print(f"    转换后: {new_path}")
            else:
                conversion_stats['unchanged'] += 1
            
            updated_rows.append(tuple(row_list))
        
        # 插入数据到目标表
        print("插入数据到新数据库...")
        placeholders = ', '.join(['?' for _ in columns])
        insert_sql = f"INSERT INTO {table_name} VALUES ({placeholders})"
        target_cursor.executemany(insert_sql, updated_rows)
        
        # 提交事务
        target_conn.commit()
        
        # 验证数据
        target_cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        target_count = target_cursor.fetchone()[0]
        
        print("\n" + "="*50)
        print("转换完成！")
        print(f"源数据库: {source_db}")
        print(f"目标数据库: {target_db}")
        print(f"处理的表: {table_name}")
        print(f"路径列: {path_column}")
        print(f"总记录数: {conversion_stats['total']}")
        print(f"已转换路径: {conversion_stats['converted']}")
        print(f"未更改记录: {conversion_stats['unchanged']}")
        print(f"目标数据库记录数: {target_count}")
        
        if conversion_stats['total'] == target_count:
            print("✓ 数据完整性验证通过")
        else:
            print("⚠ 警告: 源和目标记录数不匹配")
        
        # 显示一些示例数据
        print("\n示例数据（转换后前5条）:")
        target_cursor.execute(f"SELECT rowid, {path_column} FROM {table_name} LIMIT 5")
        sample_data = target_cursor.fetchall()
        for rowid, path in sample_data:
            print(f"  [{rowid}] {path}")
        
        # 关闭连接
        source_conn.close()
        target_conn.close()
        
        return True
        
    except sqlite3.Error as e:
        print(f"数据库错误: {e}")
        return False
    except Exception as e:
        print(f"处理错误: {e}")
        return False


def main():
    """主函数，演示如何使用转换功能"""
    
    # 设置文件路径
    current_dir = Path(__file__).parent
    source_db = current_dir / "photos.db"
    target_db = current_dir / "photosnas.db"
    
    print("="*60)
    print("照片数据库路径转换工具")
    print("="*60)
    print(f"工作目录: {current_dir}")
    print(f"源数据库: {source_db}")
    print(f"目标数据库: {target_db}")
    print("转换规则:")
    print("  1. 将路径中的 '\\feiniu' 替换为 '/vol2'")
    print("  2. 将所有反斜杠 '\\' 替换为正斜杠 '/'")
    print("="*60)
    
    
    # 执行转换
    success = convert_photo_paths(
        source_db=str(source_db),
        target_db=str(target_db),
        table_name="photo_scores",  # 根据历史对话，假设表名为photo_scores
        path_column="path"          # 假设路径列名为path
    )
    
    if success:
        print(f"\n✓ 转换成功！新数据库已保存到: {target_db}")
    else:
        print("\n✗ 转换失败")





if __name__ == "__main__":
    main()
