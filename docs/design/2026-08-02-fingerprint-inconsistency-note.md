# 隐患记录:浏览器指纹自相矛盾(暂不处理)

日期:2026-08-02
状态:**已确认存在,决定暂不处理**。本文是留档,不是待办。

---

## 现象

`app/browser/fingerprint.py` 的 `get_fingerprint(account_id)` 为每个账号生成一整套指纹,
其中包含 `webgl_renderer` / `webgl_vendor`。但 `app/browser/sync_client.py` 起 camoufox 时
**只传了其中一部分**:

```python
"navigator.userAgent": fp.user_agent,
"screen.width" / "screen.height": fp.screen_resolution,
"navigator.hardwareConcurrency": fp.hardware_concurrency,
"navigator.platform": fp.platform,
window=(fp.viewport["width"], fp.viewport["height"]),
locale / timezone_id
```

**`webgl_renderer` 与 `webgl_vendor` 从未被传给浏览器——它们是死字段。**

与此同时同一处有:

```python
block_webgl=False,  # headed 跑在真屏 :0 + RTX 4090:放开 WebGL 走真 GPU
                    # 硬件渲染(真 NVIDIA 指纹),而非 Xvfb 软件渲染/headless 特征
```

## 后果:物理上不可能的设备组合

以 NBDpsy-聊创伤 为例,指纹表生成的是:

```
platform          MacIntel
user_agent        Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:135.0) Firefox
webgl_renderer    ANGLE (Apple, Apple M1 Max, OpenGL 4.1)   ← 生成了但没传
```

而浏览器实际对外呈现的 WebGL 是**这台机器真实的 NVIDIA RTX 4090(Linux)**。

于是页面看到的是:**一台自称 macOS / MacIntel 的设备,WebGL 渲染器却是 Linux 上的
NVIDIA RTX 4090**。Mac 十余年前就不再使用 NVIDIA 显卡,M 系更是完全自研 GPU
——这不是"参数可疑",是**物理上不可能的组合**,而检测此类矛盾是反自动化系统的基本能力。

七个账号**全部**存在这个矛盾(声称 Mac 或 Windows,实际 WebGL 均为 RTX 4090)。

## 为什么暂不处理

**它解释不了已观察到的现象。** 2026-07-31 有两个账号(NBDpsy-聊创伤、NBDpsy-亲密关系)
被弹扫码验证墙,而 NBDpsy 主号、NBDpsy-我们都有病、NBDpsy-好好生活 在**同样的矛盾下**
访问同样的他人主页,一次都没撞过。七号皆有此矛盾而只有两号撞墙,**说明它至多是抬高了
风险基线,不是触发原因**。

**动它本身有风险。** 修正方向无非两条,各有代价:

- 把 `webgl_renderer`/`webgl_vendor` 真正传给 camoufox,让指纹自洽 —— 但这等于让平台
  看到"这台设备换了显卡",对已有稳定历史的账号可能反而触发校验;
- 改成 `block_webgl=True` 让 WebGL 不可用 —— 矛盾消失,但"没有 WebGL"本身也是特征,
  且当初放开正是为了走真 GPU 硬件渲染、避免软件渲染/headless 特征,是有意取舍。

七个账号目前**都在正常工作**(含两个撞过墙的号,连日互动任务全部成功),
在没有证据表明此矛盾造成实际损害之前,不值得为它承担改动风险。

## 什么情况下应当重新评估

**若历史稳定的老号(NBDpsy 主号 / NBDpsy-我们都有病 / NBDpsy-好好生活)也开始撞验证墙,
这是第一个该查的地方。** 届时说明风险基线已经抬到了触发阈值,矛盾从"隐患"变成"成因"。

另外,若将来把浏览器从这台带 RTX 4090 的机器迁走,或改为 headless/Xvfb 运行,
本节的结论需要重新计算——矛盾的具体形态会变。

## 顺带:两个死字段应当标注

`fingerprint.py` 里 `webgl_renderer` / `webgl_vendor` 生成了却无人消费。
**不要删**(将来修正指纹时要用),但建议在定义处加注释写明"当前未传给 camoufox,
见本文档",避免后人误以为 WebGL 已被指纹接管。
