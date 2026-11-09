# 更新日志

本项目按「改了什么 + 为什么」记录，不按版本号堆功能。所有日期为实际改动日期。

## v0.9.37 — 2026-11-09 · 抽帧双写：workspace + media/onebot

v0.9.36 把输出目录改到 `workspace` 并重启了服务，但**新抽的帧只有 workspace 那一份**——
`media/onebot` 里的是之前手工拷的旧图。她要是只认 media/onebot，新帧照样看不到。

改成**双写**：每抽一帧同时落两处

```
/opt/dsh/workspace/bili-frames/ss<season>/ep<N>/txxx.jpg     他读得到
/opt/dsh/media/onebot/bili-frames/ss<season>/ep<N>/txxx.jpg  她读得到
```

实测（重启后新进程 21:50:18）：

```
bili_frame_grab(ep=6, 0–40s) → t00010.jpg  workspace=True  media/onebot=True
                              t00030.jpg  workspace=True  media/onebot=True
```

两个目录各 1156 张既有图也都在。

## v0.9.36 — 2026-11-09 · 改完路径没重启服务，她跑的还是旧代码
