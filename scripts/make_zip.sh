#!/data/data/com.dsharnessmobile.shell/files/usr/bin/sh
# 打交付 zip：包含插件自带 ffmpeg（vendor 不进 git，但必须进 zip）。
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME=astrbot_plugin_ai_mind
OUT=$(cd "$ROOT/.." && pwd)/$NAME.zip
rm -f "$OUT"
cd "$ROOT/.."
zip -q -X -r "$OUT" "$NAME" \
  -x "$NAME/.git/*" \
  -x "$NAME/data/*" \
  -x "*/__pycache__/*" -x "*.pyc" -x "*.zip" -x "*.DS_Store"
ls -lh "$OUT"
