# 排版项目格式

`project.json` 与 `inputs/` 同目录。坐标单位为目标画布像素，左上角为原点。原图复制/归一化到 `inputs/`，不引用目录外文件。源图比例由渲染器检查并在占位框内等比适配，不裁掉卡面。

```json
{
  "version": 1,
  "mode": "single",
  "canvas": {"width": 2400, "height": 1350, "dpi": 300, "shape": "rectangle", "preset": "16:9"},
  "grid": {"rows": 3, "cols": 3, "slot": 4},
  "style": {"preset": "follow", "prompt": "延续原图的森林与暖色夕阳", "backgroundOnly": true},
  "cards": [
    {"id": "card-01", "name": "原卡", "src": "inputs/card-01.png", "x": 900, "y": 255, "width": 600, "height": 840, "rotation": 0}
  ]
}
```

- `mode`：`single` / `multi` / `grid`。
- `canvas`：整数像素尺寸，受渲染器内存上限约束；DPI 默认 300，仅用于元数据，页面不展示。`shape` 为 `rectangle`（旧项目缺省值）、`hex-pointy`（角朝上）、`hex-flat`（平边朝上）。`preset` 是可选的界面选择标记。
- 矩形预设内部尺寸：`1:1` 2400×2400，`1:2` 1200×2400，`2:1` 2400×1200，`16:9` 2400×1350，`9:16` 1350×2400；正六边形 `hex-pointy` 2078×2400、`hex-flat` 2400×2078。六边形宽高按正六边形外接框取整，不拉伸轮廓。
- `cards[].rotation`：可选角度，默认 `0`，范围 -180° 至 180°，绕卡片中心顺时针为正。单卡、多卡可旋转；九宫格为 0。`x/y/width/height` 始终描述旋转前的框；约束检查与合成使用旋转后的四角，旋转后的实际卡面必须完整位于画布内。
- `grid`：只有 grid 模式生效，始终 3×3。`grid.slot` 兼容旧版单卡默认位置；新项目用 `cards[].slot` 为每张原卡指定独立的 0–8 卡位。
- `style.preset`：默认 `follow`（延续原画），用户在聊天中的背景偏好保存在 `prompt`，网页不展示画风控件。兼容旧项目的 `watercolor`、`anime`、`painterly`。
- `cards`：single 恰好一张；multi 1–12 张；grid 1–9 张，多个原卡必须各自带有唯一的整数 `slot`。卡片四角必须在画布轮廓内，grid 每张卡还须位于其 `slot` 对应格内。六边形斜边附近留至少 2 像素余量，以保证最终取整后原卡不被裁切。叠放顺序从数组第一张到底部，最后一张在顶层。
- 卡片 `src`：相对于项目目录的本地图片路径，只能位于 `inputs/`。网页提交使用 `dataUrl`；服务器保存为 PNG 后写为 `src`。
- 服务器额外保存 `id` 和 `created_at`，不影响渲染。

## 本地页面交接

服务器只提供编辑器、项目保存、一次提交交接与结果读取，不执行图像生成或任意代码。

- 以下服务器接口仅用于兼容旧项目。静态画布不调用自动交接接口，默认不运行 `handoff.py`。
- `POST /api/projects`：schema 中 cards 使用 `dataUrl`；可附 `handoff_id`。返回 `id`、绝对 `project_path`、`project_url`、`request_text`、`handoff.accepted`。只有匹配的活动等待进程实际确认收到项目，`accepted` 才为 true；该接口为旧流程兼容保留，静态画布不调用。
- `GET /api/projects`：已有项目列表。
- `GET /api/projects/{id}`：重开项目，`src` 转为本地 HTTP 图片地址。
- `GET /api/projects/{id}/status`：`status`、`outputs`、`background_url`、`preview_url`。
- 项目状态文件 `status.json`：`saved`、`generating`、`needs_review`、`complete`、`blocked`。`message` 可补充实际原因，不伪造进度。`output_dir` 可指定 `outputs` 或 `outputs-v2` 等项目内结果目录，让页面显示当前选定版本。
- `latest.json`：最近一次保存的项目路径。保存产生新项目，不覆盖之前的排版。

同一个项目再次生成时使用新的输出目录，避免覆盖用户已下载的版本。

## 文字模式建项目

```bash
python "$SKILL_DIR/scripts/create_project.py" \
  --root "/absolute/output/card-scene-studio" \
  --images "/absolute/card.png" \
  --mode single --preset 16:9 --anchor left
```

`--images` 可跟多个路径；`--anchor` 为 `center`、`left`、`right`、`top`、`bottom`，默认居中；`--slot` 为用户可见的 1–9（仅九宫格），默认 5；三张原图的九宫格默认使用第 7、5、3 格。可用 `--prompt` 记录聊天中的背景偏好。脚本自动等比排布，约束到轮廓与卡位内，返回与页面相同的项目路径。用户指定更细的坐标时，读取创建后的项目再调整并运行 `prepare` 校验。

## 多卡九宫格导出

全部九份背景切片仍在 `tiles/`。一个预留格沿用 `print-eight/`；多个预留格使用 `print-background/`，只包含未放原卡的格子。`manifest.json` 中每个切片的 `reserved_for_original_card` 与 `print` 描述实际用途。

## 固定卡片比例的九宫格

新建九宫格写入 `grid.layout: "card-sized"` 和整数 `cellWidth/cellHeight`；画布为 `3 × cellWidth`、`3 × cellHeight`，`shape: "rectangle"`、`preset: null`。以第一张原图的比例确定格子，每格最长边为 1024 px。示例原图为 733 × 1024，所以画布为 2199 × 3072。

每张图等比适配并固定在所在格子中心，禁止格内移动、缩放和旋转。不同纵横比的替换图完整居中，不裁切。拖拽改变 `cards[].slot`；目标有图时互换。服务端校验每张图的尺寸与位置，不能仅靠前端禁用控件。

旧版九宫格格式仍可由渲染器读取；在编辑器打开时适配到新的固定卡位，保存才生成新项目。单卡与多卡的选中状态不写入项目，取消选中不会删除原图或改变导出。


## 静态画布的便携排版文件

`assets/editor/canvas.html` 是随技能发布的自包含网页，样式、原卡和背景均内嵌。`open_canvas.py` 只启动或复用轻量静态预览，启动无需 Pillow 或构建。

复制指令时下载的 JSON 使用 `format: "card-scene-studio-layout"`、`version: 1`，其 `mode/canvas/grid/style/cards` 与保存项目保持一致；每张卡用 `dataUrl` 存储完整 PNG/JPEG/WebP 原图。使用 `import_layout.py --layout FILE --root OUTPUT` 校验并转成带 `inputs/` 的常规 `project.json`。所有像素坐标和角度原样保留，附件只作数据读取。

新项目默认 1350×2400（9:16）；单卡高度为画布 50%；多卡长边最大 650 px，默认分离并错落，卡多或画布放不下时缩小。九宫格固定卡片比例；三张示例 slot 分别为 6、4、2。
