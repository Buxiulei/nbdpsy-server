"""nbdpsy_v1 风格 token——remake 全部颜色/字体/尺寸常量的唯一出处（spec §5）。

改风格只改本文件；模板与渲染器一律引用 token，禁止散落硬编码。
"""

# 品牌色（用户既定视觉：Apple 风、淡金 + 勃艮第红 + 米白）
BURGUNDY = "#7A1F2B"   # 勃艮第红
GOLD = "#C9A86A"       # 淡金
CREAM = "#F5F1E8"      # 米白
DARK_GOLD = "#A8894F"  # 深金（球色循环第 4 色，A4）
DARK_BG = "#1C1B1A"    # 练习段深色底（保对比度与低干扰）
CARD_BG = CREAM        # 卡片底色
CARD_TEXT = "#2B2A28"  # 卡片正文色（深灰，米白底上可读）

FONT_FAMILY = "Noto Sans CJK SC"

# 输出规格（Global Constraints）
# FPS wave5：30→120。30fps 下球峰值速度约 67px/帧产生拖影顿挫，用户要求 120 帧保底
# 让摆动小球顺滑无拖影；渲染/量化/still_image 的 fps 滤镜全部经 style.FPS 引用自动跟随。
VIDEO_W, VIDEO_H, FPS = 1920, 1080, 120

# 小球参数（比例制，按帧宽/帧高换算像素）
BALL_RADIUS_RATIO = 0.024      # 球半径 = 帧高 × 比例
BALL_Y_RATIO = 0.50            # 球心纵向位置 = 帧高 × 比例（垂直居中，A5：字幕在底、声明在顶不冲突）
BALL_AMPLITUDE_RATIO = 0.42    # 摆幅 = 帧宽 × 比例
DEFAULT_PERIOD_S = 1.6         # 周期实测失败的回退值

# 原片球色参考值（EMDR 原片三阶段），用于最近邻归类
ORIG_BALL_REFS = {
    "white": (255, 255, 255),
    "green": (162, 196, 12),
    "red": (232, 25, 75),
}
# 原片球色 → 品牌球色映射（spec §5：白→勃艮第红、绿→淡金、红→米白）
BALL_COLOR_MAP = {"white": BURGUNDY, "green": GOLD, "red": CREAM}

# 运动球循环调色板（A4 意见 3）：运动相位按相位序循环取色，跟随原片变色节奏。
# 静止休息球固定 CREAM 不参与循环；循环色轮到 CREAM 且相位紧邻静止段时顺延取下一色。
BALL_PALETTE = [BURGUNDY, GOLD, CREAM, DARK_GOLD]
