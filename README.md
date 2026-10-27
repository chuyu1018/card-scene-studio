# 卡景工坊 · Card Scene Studio

给卡牌排版，再围绕原图生成一张连续背景。支持单卡、多卡共景、九宫格，以及矩形和正六边形画布。

原卡保持独立：纯背景和打印切片不含原卡；合成预览按排版坐标叠回原图。网页负责排版，图片生成在聊天中完成。

## 安装

把完整仓库安装到本机 skills 目录，不要只下载 `SKILL.md`：

```bash
git clone https://github.com/chuyu1018/card-scene-studio.git ~/.codex/skills/card-scene-studio
```

如果这个目录已经存在，先保留自己的版本，再安装或合并更新。重新打开聊天后，调用 `$card-scene-studio`，或选择「卡景工坊」。运行环境需要支持本地 skill、文件操作和原生图片生成。

## 打开画布

在聊天中发送：

```text
$card-scene-studio 打开画布
```

也可以手动启动本机预览：

```bash
python3 ~/.codex/skills/card-scene-studio/scripts/open_canvas.py
```

打开命令返回的 URL。完整网页已经打包在 `assets/editor/canvas.html`，启动不需要 Node、Pillow、构建或外部服务。预览只监听本机，只提供这张网页；图片在浏览器内导入和排版。

1. 选择首页示例，或上传自己的卡牌。首页悬浮、键盘聚焦时显示扩图示例。
2. 拖动调整位置；单卡、多卡通过右下角方形把手旋转和等比缩放。支持中心线吸附、零度吸附、撤销和重做。
3. 点击顶部「复制生成指令」。页面会复制 prompt，并下载包含原图和排版数据的 JSON 文件。
4. 把指令粘贴到 ChatGPT，并附上下载的排版文件。按钮不会自动发送消息或开始生图。

单卡默认 9:16，卡片高度占画布一半。多卡默认长边 650 px，错落摆放且不重叠，空间不足时等比缩小。九宫格每格保持卡片比例，三张示例按左下、中央、右上排列。位置、尺寸和角度默认收起，点击「卡片参数」展开。

## 生成与导出

图像处理脚本需要 Python 3.10+ 和 Pillow。优先使用聊天应用自带的 Python；手动配置时可使用独立虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

技能会读取排版附件、恢复原图与坐标，生成环境背景，再导出：

- `background.png`：纯背景整图。
- `background-cutout.png`：原卡位置透明的背景。
- `composite-preview.png`：叠回原卡的效果预览。
- 九宫格全部 9 张切片，以及排除原卡所在格的打印背景片。
- `manifest.json` 和 `outputs.zip`。

生成需要当前聊天可用的图片工具。本地脚本不调用额外的图片 API。背景接缝仍需逐张检查；原卡位置准确，不代表周围景物一定无缝。判断方法见 [接缝诊断与修复](references/seam-continuity.md)。

## 维护

修改网页源码或示例素材后，重新打包再提交 `canvas.html`：

```bash
.venv/bin/python scripts/build_editor.py
.venv/bin/python -m unittest discover -s tests
node tests/editor_geometry.cjs
```

`scripts/server.py` 和 `scripts/handoff.py` 保留用于兼容旧项目，默认启动流程不使用它们。

## 文件与素材

- [SKILL.md](SKILL.md)：完整生成流程。
- `assets/editor/`：网页源码、可直接使用的打包网页及示例素材。
- `scripts/`：启动、排版导入、背景准备、合成和切片。
- `references/`：项目格式、纯背景流程和接缝检查。
- `tests/`：排版、导入导出和渲染验证。

示例卡图片来源、插画作者和校验值见 [demos/sources.json](assets/editor/demos/sources.json)。首页背景由原生图片工具生成，提示词和来源记录在 [scenes/](assets/editor/scenes/)。宝可梦相关图像和商标归各自权利人所有，本项目与官方无关联。
