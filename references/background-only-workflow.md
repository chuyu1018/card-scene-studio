# 纯背景流程

目标是生成实际可用的空场景，原卡保留在独立合成预览。图片生成与修改均使用会话原生 image_gen；脚本只负责几何定位、合成、验证和导出。

## 1. 从完整原图生成空场景

先查看原图，使用未经遮挡或打码的完整原图作为编辑参考。诚实说明它是卡牌插画，指定真实角色和环境；不用代码覆盖角色，也不靠删词或隐藏来源改变审核结果。

提示词模板（把尺寸、来源与环境细节替换为实际观察）：

```text
Use case: precise-object-edit, background extraction.
The input is an original Pokémon trading card. Produce ONLY its empty illustrated environment.
Remove all Pokémon characters, including small portrait icons, and all printed card text,
symbols, numbers, borders and UI. Reconstruct the surrounding scenery in those areas.
Keep the environment at its current position and scale on the ORIGINAL [width:height] canvas.
Do not zoom, crop, recenter, rescale, or extend the field of view at this stage.
Preserve existing environmental contours, edge crossing heights, palette, light and painted texture.
[Describe the actual cliffs, sky, plants, terrain and their locations.]
Return one fully opaque environment image without characters, text, borders, panels or blank regions.
```

用 `transparent_background:false`。将实际返回的图片、提示词、原始参考路径及哈希、工具输出路径另存。核对空场景中的岩层、植物、天空开口与原图的位置；即使角色已经移除，景物位移仍须记为质量问题。

**扩图前检查边界。** 将空场景与原卡按同一尺度对照，检查边缘附近可见景物的交点、方向、颜色和纹理。严格接缝任务中，已经改形、改色的底图先按 [接缝诊断与修复](seam-continuity.md) 处理，不能只因角色移除成功就继续扩图并标为精确结果。文字和卡框遮住的原始环境是未知区域，生成内容只能记作推测补全。

## 2. 按真实卡位准备扩图参考

为每张原卡建立一项映射。`src` 相对映射文件所在目录或为绝对路径；`source_sha256` 必须与当前完整原卡一致。底图须完全不透明、比例与原卡相符，不能用拉伸补齐。

```json
{
  "version": 1,
  "environments": [
    {
      "card_id": "card-1",
      "source_sha256": "原卡文件的实际 SHA256",
      "src": "clean-scenes/charmander.png",
      "prompt": "本次实际使用的完整提示词",
      "generation_source": "工具返回的真实本地输出路径"
    }
  ]
}
```

```bash
python "$SKILL_DIR/scripts/render.py" prepare \
  --project "/absolute/project.json" \
  --environments "/absolute/generated/environments.json" \
  --output "prepare-scenes-01"
```

环境片段与原卡共用尺寸、中心、旋转、取整和叠放顺序。先查看返回的排版参考图，再调用原生生图。新的空白画布区域仅表示待扩展范围；它不是对原图角色的遮挡。

## 3. 将环境扩展成一张完整背景

读取命令返回的请求文件，强调全画布尺寸、每个片段的准确位置与占比，以及跨边界的岩层走向、明暗、地平线。不得把片段放大铺满画布。多卡和九宫格都在同一全画布上完成扩图；不能分别生成多个完整背景再拼成假共景。

输出仍必须是完整、不透明的环境，不含角色、卡牌、文字、边框或留位孔洞。模型没有暴露硬性区域锁定参数时，不声称能锁住像素。提示词写了“保持不变”也不构成验证证据。

出现接缝时，先区分整体偏移与景物被重画，再选择配准或局部结构修复；轮廓接上后才考虑色彩融合。保留完整场景上下文，避免把每条接缝分别生图后拼成互相矛盾的环境。窄带宽度按实际分辨率和偏移选择，不把实验中的固定像素数当作通用参数。修复后重新叠回真实原卡并逐边验收；出现晕边、岩壁拉弯或重复景物的版本不作为成功输出。

## 4. 原卡合成与验收

使用原有 `render.py finish`，原卡只叠到 `composite-preview.png`。背景、透明留位背景及打印切片各自保留。

每次至少记录以下结果：

```json
{
  "generation_returned": true,
  "background_only_pass": true,
  "edge_continuity_pass": false,
  "status": "preview",
  "notes": ["逐边填写实际观察到的景物偏移、色差或纹理断点"]
}
```

出图、纯背景、接缝连续是三个不同结论。使用固定流程连续三次的要求，只能由三次实际生成并分别验收证明；复用一张底图的三次扩图，应明确是扩图阶段三次，不是全流程三次。原卡叠回正确、切片重组一致和文件完整性均不能替代接缝检查。

工具若拒绝某次请求，记录真实错误并停止该生成，不以遮脸、换接口或隐藏来源重试。技术错误与画面质量问题另行记录。三张完整原卡成功生成空场景，只证明这些样本能执行该内容任务，不能推出普遍审核通过率或连续性保证。
