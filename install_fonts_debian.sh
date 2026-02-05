#!/bin/bash
# Debian 12 中文字体安装脚本

echo "正在安装 Debian 12 中文字体..."

# 更新包列表
sudo apt update

# 安装中文字体包，包括楷体
sudo apt install -y fonts-noto-cjk fonts-noto-color-emoji fonts-wqy-microhei fonts-wqy-zenhei fonts-liberation fonts-arphic-gkai00mp fonts-arphic-bsmi00lp fonts-arphic-ukai fonts-arphic-uming

# 检查是否安装成功
if [ -f "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc" ]; then
    echo "✓ Noto CJK 字体已安装"
else
    echo "⚠ Noto CJK 字体未找到，尝试安装其他字体"
    sudo apt install -y fonts-noto-cjk-extra
fi

if [ -f "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc" ]; then
    echo "✓ 文泉驿微米黑字体已安装"
else
    echo "⚠ 文泉驿微米黑字体未找到"
fi

if [ -f "/usr/share/fonts/truetype/arphic/gkai00mp.ttf" ]; then
    echo "✓ AR PL 楷体已安装"
else
    echo "⚠ AR PL 楷体未找到"
fi

# 更新字体缓存
sudo fc-cache -fv

echo "字体安装完成！"
echo "请重启您的应用程序以使用新安装的字体。"