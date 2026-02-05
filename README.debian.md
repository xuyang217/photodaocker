# InkTime Photo Server - Debian 12 部署指南

## 系统要求

- Debian 12 (Bookworm)
- Python 3.8+
- pip

## 安装步骤

### 1. 安装系统依赖

```bash
# 更新系统包列表
sudo apt update

# 安装 Python 和 pip（如果尚未安装）
sudo apt install python3 python3-pip

# 安装中文字体（重要：解决文字不显示问题）
chmod +x install_fonts_debian.sh
./install_fonts_debian.sh
```

### 2. 安装 Python 依赖

```bash
pip3 install flask pillow
```

### 3. 配置文件设置

复制配置文件模板并根据您的环境进行修改：

```bash
cp config.debian.py config.py
```

编辑 `config.py` 文件，设置以下关键参数：
- `IMAGE_DIR`: 您的照片目录路径
- `DB_PATH`: 数据库文件路径
- `FONT_PATH`: 字体路径（可选，留空则自动查找系统字体）

### 4. 运行服务

```bash
python3 server.py
```

服务将在 `http://0.0.0.0:8765` 上运行。

## 故障排除

### 文字不显示问题

如果照片上的文字仍然不显示，请检查：

1. 确认已安装中文字体：
   ```bash
   fc-list :lang=zh
   ```

2. 检查日志输出，确认字体加载情况：
   ```bash
   python3 server.py
   ```

3. 手动指定字体路径，在 `config.py` 中设置 `FONT_PATH`：
   ```python
   FONT_PATH = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
   ```

### 楷体字体安装（推荐）

如果您希望使用楷体显示日期和文案，请安装楷体字体：

```bash
# 安装楷体字体包
sudo apt install fonts-arphic-gkai00mp fonts-arphic-bsmi00lp

# 或者安装更多中文字体
sudo apt install fonts-wqy-zenhei fonts-wqy-microhei fonts-arphic-ukai fonts-arphic-uming
```

### 权限问题

确保您的照片目录具有适当的读取权限：
```bash
chmod -R 755 /path/to/your/photos
```

### 网络访问

如果需要从外部网络访问，请确保防火墙设置允许 8765 端口：
```bash
sudo ufw allow 8765
```

## 功能说明

- `/review` - 浏览和管理所有照片
- `/today-image` - 查看今日推荐的照片
- `/sim` - 照片模拟器预览
- `/files/` - 输出文件浏览