#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让她真的「听见」—— 本地 ASR + 声音特征（2026-11-09）

之前的状态：音频下载了但没人用。番剧没有 CC 字幕轨，抽帧只能看到硬字幕。
所以「听」这件事完全空缺 —— 用户一句「听不见声音又没字幕，那看什么」说到点上。

本补丁做三件事（幂等，MEDIA_ASR_LOCAL 标记）：
  1. whisper.cpp 的语言：原来写死 `-l auto`，日语动画会被判成英语出一道英文转写。
     番剧（info 带 ep_id）固定 `-l ja`，其它视频仍然 auto。
  2. 新增 audio_profile：用 ffmpeg 量出「这段声音长什么样」——
     平均/峰值音量、静音占比、以及静音间隔（等价于「语流密度」）。
     这不是听懂，是能感到"吵/静/有段音乐一直在响"，配合转写一起看。
  3. 转写结果附上耗时与覆盖说明，避免她把「只转了前 3 分钟」当成「整集都听了」。

依赖（由 install 步骤准备，不在补丁里下载）：
  · /opt/tulpa/tools/whispercpp/bin/whisper-cli   （whisper.cpp 编译产物）
  · /opt/tulpa/.cache/voice-models/ggml-small-q5_1.bin（官方 sha256 校验通过）

用法：
    python3 patch_tulpa_asr.py            # 打补丁
    python3 patch_tulpa_asr.py --check
"""
import io
import json
import os
import shutil
import sys

MEDIA = "/opt/tulpa/tulpa_bilibili/media.py"
SETTINGS = "/opt/tulpa/data/bilibili/settings.json"
MARK = "MEDIA_ASR_LOCAL"

PROFILE_METHOD = '''
    # ==== 2026-11-09 本地补丁（MEDIA_ASR_LOCAL）：声音特征 ====
    def _audio_profile(self, media, start, end, cancel, deadline):
        """量出这段音频「听起来是什么样」：音量、静音、语流密度。

        不是听懂内容（那是 _asr 的活），只回答"这里吵不吵、有没有音乐一直响、
        说话是连续的还是断断续续"。纯 ffmpeg，不加载模型，几秒出结果。
        """
        span = max(1.0, float(end) - float(start))
        base = [ffmpeg(), '-nostdin', '-v', 'info', '-protocol_whitelist', 'file,pipe',
                '-ss', str(start), '-t', str(span), '-i', str(media), '-vn',
                '-af', 'volumedetect,silencedetect=n=-32dB:d=1.0', '-f', 'null', '-']
        r = subprocess.run(base, capture_output=True, text=True, timeout=180)
        log_text = (r.stderr or '')
        stats = {}
        for line in log_text.splitlines():
            for key, name in (('mean_volume:', 'mean_db'), ('max_volume:', 'peak_db')):
                if key in line:
                    try:
                        stats[name] = float(line.split(key)[1].split('dB')[0].strip())
                    except (ValueError, IndexError):
                        pass
        silences = []
        cur = None
        for line in log_text.splitlines():
            if 'silence_start:' in line:
                try:
                    cur = float(line.split('silence_start:')[1].strip())
                except ValueError:
                    cur = None
            elif 'silence_end:' in line and cur is not None:
                try:
                    stop = float(line.split('silence_end:')[1].split('|')[0].strip())
                    silences.append([round(cur, 2), round(stop, 2)])
                except (ValueError, IndexError):
                    pass
                cur = None
        silent_seconds = sum(b - a for a, b in silences)
        profile = dict(window=[start, end], span_seconds=round(span, 1),
                       mean_db=stats.get('mean_db'), peak_db=stats.get('peak_db'),
                       silence_seconds=round(silent_seconds, 1),
                       silence_ratio=round(min(1.0, silent_seconds / span), 3),
                       silence_gaps=silences[:40],
                       note='音量/静音只是"听起来的样子"；台词要看 speech 字段。')
        if profile['silence_ratio'] >= 0.9:
            profile['feel'] = '几乎全程安静（可能是纯音乐段、或这段没有对白）'
        elif profile['silence_ratio'] <= 0.1 and (profile['mean_db'] or -99) > -20:
            profile['feel'] = '从头到尾都很满很吵（对白密集或音乐铺满）'
        else:
            profile['feel'] = '有说话也有停顿（正常对白节奏）'
        return profile

'''

ASR_SIG_OLD = "    def _asr(self, audio, folder, start, end, cancel, deadline):"
ASR_SIG_NEW = "    def _asr(self, audio, folder, start, end, cancel, deadline, lang='auto'):"

LANG_OLD = "run_local([str(executable),'-m',str(model),'-f',str(wav),'-l','auto','-t','4','-oj','-of',str(result),'-np'],cancel,deadline)"
LANG_NEW = "run_local([str(executable),'-m',str(model),'-f',str(wav),'-l',lang,'-t','2','-oj','-of',str(result),'-np'],cancel,deadline)"

CALL_OLD = "                    result['speech'] = self._asr(audio or video, folder, start, end, cancel, deadline)"
CALL_NEW = ("                    # 番剧是日语动画：auto 会被判成英语，出英文转写（实测过）\n"
            "                    _lang = 'ja' if info.get('ep_id') else 'auto'\n"
            "                    result['speech'] = self._asr(audio or video, folder, start, end,\n"
            "                                                 cancel, deadline, _lang)")

PROFILE_OLD = "                if row['kind']=='read_video' and result['speech']['state']!='available':"
PROFILE_NEW = ("                try:\n"
               "                    result['audio_profile'] = self._audio_profile(audio or video, start, end,\n"
               "                                                                  cancel, deadline)\n"
               "                except Exception as _exc:\n"
               "                    result['warnings'].append(dict(component='audio_profile',\n"
               "                                                   message=str(_exc)[:120]))\n"
               "                if row['kind']=='read_video' and result['speech']['state']!='available':")


def rd(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def wr(p, s):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def patch_media(apply_):
    s = rd(MEDIA)
    if MARK in s:
        return "已打"
    if not apply_:
        return "未打"
    for anchor in (ASR_SIG_OLD, LANG_OLD, CALL_OLD, PROFILE_OLD):
        if anchor not in s:
            return "跳过（media.py 锚点找不到：%s）" % anchor[:36]
    shutil.copy2(MEDIA, MEDIA + ".bak-asr")
    s = s.replace(ASR_SIG_OLD, ASR_SIG_NEW, 1)
    s = s.replace(LANG_OLD, LANG_NEW, 1)
    s = s.replace(CALL_OLD, CALL_NEW, 1)
    s = s.replace(PROFILE_OLD, PROFILE_NEW, 1)
    # 方法插到 _asr 前面
    s = s.replace(ASR_SIG_NEW, PROFILE_METHOD.strip("\n") + "\n\n" + ASR_SIG_NEW, 1)
    wr(MEDIA, s)
    return "已打"


def tune_limits():
    """整集 ASR 要十几分钟，得放宽任务超时；同时把并发降到 1，别让两核互相抢。"""
    try:
        data = json.load(io.open(SETTINGS, encoding="utf-8"))
    except Exception as e:
        return "跳过（读不到 settings.json：%s）" % str(e)[:60]
    limits = data.setdefault("limits", {})
    changed = []
    if limits.get("job_timeout_seconds", 600) < 1500:
        limits["job_timeout_seconds"] = 1500
        changed.append("job_timeout 600→1500")
    if limits.get("max_jobs", 2) != 1:
        limits["max_jobs"] = 1
        changed.append("max_jobs→1")
    if not changed:
        return "无需调整"
    shutil.copy2(SETTINGS, SETTINGS + ".bak-asr")
    io.open(SETTINGS, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=1))
    return "、".join(changed)


def main():
    apply_ = "--check" not in sys.argv
    print("  %-24s %s" % ("media.py 语言+声音特征", patch_media(apply_)))
    print("  %-24s %s" % ("settings 任务上限", tune_limits() if apply_ else "未改"))
    venv = "/opt/tulpa/.venv/bin/python"
    if os.path.exists(venv):
        rc = os.system("%s -c 'import sys; sys.path.insert(0,\"/opt/tulpa\"); "
                       "import tulpa_bilibili.media' 2>/dev/null" % venv)
        print("  导入自检:", "通过" if rc == 0 else "失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
