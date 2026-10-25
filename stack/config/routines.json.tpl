{
  "dailyBudget": 5,
  "routines": [
    {
      "id": "morning",
      "window": [
        "09:20",
        "10:40"
      ],
      "minIdleMin": 90,
      "prompt": "（早上）刚醒，脑子还在转。昨天有没有什么没记完的，或者今天想干的事？想写就写两句，不想就算了。"
    },
    {
      "id": "noon",
      "window": [
        "11:45",
        "13:10"
      ],
      "minIdleMin": 45,
      "prompt": "（午间）中午了。上午有没有值得记一笔的？有就两三句，写清楚当时什么感觉；没什么意思就别硬写。"
    },
    {
      "id": "evening",
      "window": [
        "18:40",
        "20:10"
      ],
      "minIdleMin": 45,
      "prompt": "（傍晚）一天差不多过去了，有没有什么好玩的、或者让你不爽的事，想留一句？"
    },
    {
      "id": "night",
      "window": [
        "22:30",
        "23:50"
      ],
      "minIdleMin": 60,
      "prompt": "（夜里）把今天过一遍，挑一两件真值得记的写成日记，带上你当时的心情。要是今天确实没什么，那就不写。"
    },
    {
      "id": "qzone_daily",
      "window": [
        "00:00",
        "00:25"
      ],
      "minIdleMin": 0,
      "prompt": "（零点）到零点了，发一条今天的说说。一天就这一条，挑今天最想留下的一件事或者一句话，两三句，配一张表情也行。发完不用汇报。"
    },
    {
      "id": "casual_dm",
      "window": [
        "08:30",
        "23:30"
      ],
      "minIdleMin": 60,
      "rampPerHour": 0.2,
      "rampCap": 0.8,
      "prompt": "（主动）这会儿没人找你。想跟主人说点什么就说一句，比如看到有意思的事、想起之前聊过的某件事、或者单纯问一句。不想说就别说，不用硬找话题。",
      "off": false,
      "dailyBudget": 8,
      "slotAware": true,
      "baseChance": 0.06,
      "idleBonusPerHour": 0.05,
      "idleBonusCap": 0.3
    },
    {
      "id": "casual_dm_night",
      "window": [
        "01:00",
        "06:30"
      ],
      "ignoreQuiet": true,
      "minIdleMin": 0,
      "dailyBudget": 1,
      "baseChance": 0.05,
      "idleBonusPerHour": 0.0,
      "idleBonusCap": 0.0,
      "rampPerHour": 0.05,
      "rampCap": 0.12,
      "prompt": "（半夜醒了）这个点你还醒着吗……鲸鲸睡不着，翻来覆去好久了。要是你也醒着，陪我说两句；要是你睡了，别回，我自己再躺会儿。",
      "off": false
    }
  ],
  "dmDailyBudget": 8
}
