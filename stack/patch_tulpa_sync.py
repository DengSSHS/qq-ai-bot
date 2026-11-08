#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""声音与画面对齐 —— 2026-11-09（MEDIA_SYNC_LOCAL）

问题：默认抽帧是按区间均分的（0:11 / 0:33 / 0:56 …），经常截在空镜头上；
台词虽然有毫秒时间戳，但她得自己拿两个列表对时间轴。用户一句
「声音和画面是要有同步的，至少知道哪里说了什么」正是这个缺口。

本补丁做两件事（幂等）：

  1. **_sync_frames**：等台词出来之后，挑「字最多、彼此间隔 ≥8 秒」的几句，
     在各句中间时刻补抽画面 → 每帧带 `aligns_to`（这句话就是这一帧说的）。
     补的帧仍受 max_frames 上限约束，最多 4 张。
  2. **_timeline**：把帧和台词织成一条按时间排序的时间线 ——
     每个画面点后面跟着「那一刻在说什么」，没被任何帧覆盖的台词也会列出来，
     不会因为抽帧没抽到就丢掉。

不改任何工具 schema，`get_video_job` 的 result 里多出 `timeline` 字段。

用法：python3 patch_tulpa_sync.py [--check]
"""
import io
import os
import shutil
import sys

MEDIA = "/opt/tulpa/tulpa_bilibili/media.py"
MARK = "MEDIA_SYNC_LOCAL"

GRAB = '''    # ==== 2026-11-09 本地补丁（MEDIA_SYNC_LOCAL）：抽帧与对齐 ====
    def _grab(self, video, stamp, folder, index, cancel, deadline):
        """按时间点抓一帧，返回可直接放 result 的字典。"""
        output = folder / ('frame-%d.jpg' % index)
        run_local([ffmpeg(), '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                   '-ss', str(stamp), '-i', str(video), '-frames:v', '1', '-vf',
                   'scale=%d:-2' % self.settings.limits.frame_width, '-q:v', '3', str(output)],
                  cancel, min(deadline, time.monotonic() + 30))
        if not output.exists() or output.stat().st_size > 512 * 1024:
            raise Failure('frame_failed', '该时间点没有可用画面。')
        return dict(timestamp_seconds=stamp, mime_type='image/jpeg',
                    source='decoded_video_frame',
                    data=base64.b64encode(output.read_bytes()).decode())

    def _sync_frames(self, video, result, folder, cancel, deadline):
        """把画面挪到「真的有人在说话」的时刻，并记住那一帧对应哪句台词。

        为什么：均分抽帧经常落在空镜头/转场上，她看到的是布景而不是对话。
        这里按台词长度挑几句（间隔 ≥8 秒，避免四帧全在同一段），
        在句子中间时刻补抽，并写 aligns_to。
        """
        speech = result.get('speech') or {}
        entries = speech.get('entries') or []
        if speech.get('state') != 'available' or not entries:
            return 0
        room = self.settings.limits.max_frames - len(result.get('frames') or [])
        if room <= 0:
            return 0
        ranked = sorted(entries, key=lambda e: len(str(e.get('content') or '')), reverse=True)
        picked, times = [], []
        for e in ranked:
            try:
                t = (float(e['start']) + float(e['end'])) / 2
            except (KeyError, TypeError, ValueError):
                continue
            if any(abs(t - x) < 8 for x in times):
                continue
            picked.append(e)
            times.append(t)
            if len(picked) >= min(4, room):
                break
        picked.sort(key=lambda e: float(e['start']))
        added = 0
        for e in picked:
            t = round((float(e['start']) + float(e['end'])) / 2, 3)
            frame = self._grab(video, t, folder, len(result['frames']), cancel, deadline)
            frame['source'] = 'decoded_video_frame_aligned_to_speech'
            frame['aligns_to'] = str(e.get('content') or '')[:200]
            result['frames'].append(frame)
            added += 1
        return added

    def _timeline(self, result, window=3.0):
        """织时间线：每个画面点后面跟「那一刻在说什么」。"""
        entries = (result.get('speech') or {}).get('entries') or []
        rows, used = [], set()
        for i, f in enumerate(result.get('frames') or []):
            try:
                t = float(f['timestamp_seconds'])
            except (KeyError, TypeError, ValueError):
                continue
            near = []
            for e in entries:
                try:
                    s, en = float(e['start']), float(e['end'])
                except (KeyError, TypeError, ValueError):
                    continue
                if en >= t - window and s <= t + window:
                    near.append(e)
                    used.add(id(e))
            rows.append(dict(t=round(t, 2), kind='frame', frame_index=i,
                             said=[str(e.get('content'))[:200] for e in near]))
        for e in entries:
            if id(e) in used:
                continue
            try:
                rows.append(dict(t=round(float(e['start']), 2), kind='speech',
                                 said=[str(e.get('content'))[:200]]))
            except (KeyError, TypeError, ValueError):
                continue
        rows.sort(key=lambda r: r['t'])
        return rows[:400]

'''

OLD_LOOP = """                for index, stamp in enumerate(times):
                    self.settings.assert_generation(row['generation'])
                    output = folder/f'frame-{index}.jpg'
                    run_local([ffmpeg(), '-nostdin','-v','error','-protocol_whitelist','file,pipe',
                               '-ss',str(stamp),'-i',str(video),'-frames:v','1','-vf',
                               f'scale={self.settings.limits.frame_width}:-2','-q:v','3',str(output)],
                              cancel, min(deadline, time.monotonic()+30))
                    if not output.exists() or output.stat().st_size > 512*1024:
                        raise Failure('frame_failed', '该时间点没有可用画面。')
                    result['frames'].append(dict(timestamp_seconds=stamp, mime_type='image/jpeg',
                        source='decoded_video_frame', data=base64.b64encode(output.read_bytes()).decode()))
"""

NEW_LOOP = """                for index, stamp in enumerate(times):
                    self.settings.assert_generation(row['generation'])
                    result['frames'].append(self._grab(video, stamp, folder, index, cancel, deadline))
"""

OLD_AFTER_ASR = """                    result['speech'] = self._asr(audio or video, folder, start, end,
                                                 cancel, deadline, _lang)
"""

NEW_AFTER_ASR = """                    result['speech'] = self._asr(audio or video, folder, start, end,
                                                 cancel, deadline, _lang)
                # 2026-11-09（MEDIA_SYNC_LOCAL）：台词落地后把画面也对齐过去
                try:
                    added = self._sync_frames(video, result, folder, cancel, deadline)
                    result['timeline'] = self._timeline(result)
                    result['sync'] = dict(aligned_frames=added,
                                          note='timeline 按时间排序；frame 行附该时刻在说什么，'
                                               'speech 行是没被任何画面覆盖到的台词。')
                except Exception as _exc:
                    result['warnings'].append(dict(component='timeline',
                                                   message=str(_exc)[:120]))
"""


def rd(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def wr(p, s):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def main():
    apply_ = "--check" not in sys.argv
    s = rd(MEDIA)
    if MARK in s:
        print("  media.py 声画对齐        已打")
        return 0
    if not apply_:
        print("  media.py 声画对齐        未打")
        return 0
    for anchor in (OLD_LOOP, OLD_AFTER_ASR):
        if anchor not in s:
            print("  media.py 声画对齐        跳过（锚点找不到）")
            return 1
    shutil.copy2(MEDIA, MEDIA + ".bak-sync")
    s = s.replace(OLD_LOOP, NEW_LOOP, 1)
    s = s.replace(OLD_AFTER_ASR, NEW_AFTER_ASR, 1)
    s = s.replace("    def _evidence(self, row, folder, cancel, deadline):",
                  GRAB + "    def _evidence(self, row, folder, cancel, deadline):", 1)
    wr(MEDIA, s)
    print("  media.py 声画对齐        已打")
    venv = "/opt/tulpa/.venv/bin/python"
    if os.path.exists(venv):
        rc = os.system("%s -c 'import sys; sys.path.insert(0,\"/opt/tulpa\"); "
                       "import tulpa_bilibili.media' 2>/dev/null" % venv)
        print("  导入自检:               ", "通过" if rc == 0 else "失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
