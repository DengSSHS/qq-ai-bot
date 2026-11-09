# 更新日志

本项目按「改了什么 + 为什么」记录，不按版本号堆功能。所有日期为实际改动日期。

## v0.9.38 — 2026-11-09 · 她说图还在 /opt/tulpa/.../grab/：那是重启前的旧文件

查时间戳就清楚了：

```
/opt/tulpa/data/bilibili/watch/ss73077/grab/   最后写入 21:00:44   ← 她重启前那次调用
/opt/dsh/workspace/bili-frames/ss73077         最新 21:50:29       ← 我的验证
/opt/dsh/media/onebot/bili-frames/ss73077      最新 21:50:29
```

她引用的是 21:00 那次的产物，而我是 21:48/21:52 才重启的服务。也就是说她那句话是**旧结论**，
不是新一次调用返回的路径。

不过既然她在意「哪份我能读」，我做了三件事让它不再需要猜：

1. 旧 grab 目录里的 43 张图补齐到两个可读目录（`ep-grab-legacy/`），共 86 份
2. 抽帧**双写**（workspace + media/onebot），不靠事后拷贝
3. 工具返回值新增 `mirror_dir` 字段，直接告诉她"哪份是你能读的目录"

实测（重启后 21:52:37 的新进程）：

```
bili_frame_grab(ep=2, 838–891s) → 2 张
   ss73077/ep2/t00851.jpg   workspace=True  mirror=True
   ss73077/ep2/t00877.jpg   workspace=True  mirror=True
   mirror_dir: /opt/dsh/media/onebot/bili-frames/ss73077/ep2
```

## v0.9.37 — 2026-11-09 · 抽帧双写：workspace + media/onebot
