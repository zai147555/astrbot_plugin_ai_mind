# 第三方组件与许可

本仓库自己的代码是 MIT（见 LICENSE）。下面这些是它引用/内联/随包分发的第三方内容。

## 随包分发的二进制：ffmpeg（GPLv3）

交付 zip 里的 `vendor/ffmpeg/windows/ffmpeg.exe` 是 **FFmpeg** 的静态构建，
按 **GPLv3** 授权分发（不是本项目的 MIT 代码）：

- 项目主页 / 源码：https://ffmpeg.org/
- 源码下载：https://ffmpeg.org/download.html
- 本二进制来自 Gyan Doshi 的 Windows 构建（essentials build 9.0.2）：
  https://github.com/GyanD/codexffmpeg/releases
- 版本：FFmpeg 9.0.2 essentials build（x86-64, PE32+）
- 许可全文：https://www.gnu.org/licenses/gpl-3.0.txt

重新生成（不进 git，只进交付 zip）：

```bash
python3 scripts/fetch_ffmpeg.py --platform windows --force
```

只想用系统已装的 ffmpeg：删掉 `vendor/ffmpeg/windows/` 即可，
插件会按「设置 → 插件自带 → data/bin → PATH → 常见安装位置」继续找。

## 内联的源码

| 组件 | 许可 | 用在哪 |
| --- | --- | --- |
| qrcode-generator（kazuhikoarase） | MIT | 面板「扫码登录」画二维码，已内联进 pages/mind/index.html |
| dsh-video-storyboard | Apache-2.0 | 分镜的算法与交互（mind/storyboard.py 重写实现） |
| dsh-bili-scroll | MIT | 刷 B 站（mind/bili.py） |
| dsh-knowledge-canvas | MIT | 知识树（mind/knowledge.py） |
| dsh-read-animation | MIT | 动图逐帧（mind/animation.py） |
| dsh-soundcheck | MIT | 听音频（mind/soundcheck.py） |
| dsh-unified-memory | MIT | 分层记忆（mind/memory/unified.py） |
| dsh-mo-image | MIT | 文生图适配层（mind/imagegen.py） |
| poke_to_llm | MIT | 戳一戳接话（mind/poke.py） |

只借鉴概念、没有搬运代码的项目（stealer / AIQTalk / MemOS / memewiki）在 README 里写明了原因。
