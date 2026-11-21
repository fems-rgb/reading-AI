"""绘本领域的轻量词表，供 AI 自检做"画外实体"检测。

不引入分词模型：3-6 岁绘本的高频具体名词是有限集合，
用固定词表 + 最长匹配就能覆盖绝大部分幻觉场景，且完全可解释——
老师看到"题目里出现了画面没有的『大象』"能立刻判断真假。
"""

from __future__ import annotations

import re

# 画面里"可被看见"的具体名词（动物 / 人物 / 物品 / 自然 / 场景）
PICTURE_NOUNS: tuple[str, ...] = (
    "小兔子", "兔子", "小熊", "小狗", "小猫", "小鸟", "小鸡", "小鸭", "小羊", "小猪",
    "小马", "小牛", "小鱼", "小虾", "青蛙", "蝴蝶", "蜜蜂", "蚂蚁", "毛毛虫", "乌龟",
    "松鼠", "猴子", "大象", "狮子", "老虎", "长颈鹿", "熊猫", "狐狸", "狼", "刺猬",
    "企鹅", "海豚", "鲸鱼", "恐龙", "小马", "小鹿", "小刺猬", "小老鼠", "小鹅", "小猫头鹰",
    "妈妈", "爸爸", "爷爷", "奶奶", "外婆", "外公", "哥哥", "姐姐", "弟弟", "妹妹",
    "老师", "小朋友", "孩子", "男孩", "女孩", "医生", "警察", "司机", "农民", "厨师",
    "太阳", "月亮", "星星", "云朵", "彩虹", "雨", "雪", "风", "闪电", "天空",
    "树", "大树", "小树", "花", "小花", "草", "草地", "叶子", "树叶", "种子",
    "森林", "山", "山坡", "河", "小河", "大海", "湖", "池塘", "沙滩", "岛",
    "房子", "小屋", "窗户", "门", "幼儿园", "公园", "游乐场", "学校", "医院", "商店",
    "汽车", "公交车", "火车", "自行车", "船", "小船", "飞机", "热气球", "滑梯", "秋千",
    "风筝", "球", "皮球", "气球", "积木", "娃娃", "玩具", "拼图", "陀螺", "风车",
    "帽子", "围巾", "手套", "衣服", "裙子", "裤子", "鞋子", "袜子", "书包", "雨伞",
    "书", "绘本", "画笔", "蜡笔", "纸", "剪刀", "胶水", "橡皮", "铅笔", "本子",
    "苹果", "香蕉", "西瓜", "橘子", "草莓", "葡萄", "桃子", "梨", "菠萝", "樱桃",
    "蛋糕", "饼干", "面包", "牛奶", "果汁", "糖果", "冰淇淋", "面条", "米饭", "鸡蛋",
    "桌子", "椅子", "床", "沙发", "柜子", "镜子", "灯", "钟", "杯子", "碗",
    "勺子", "筷子", "篮子", "盒子", "袋子", "绳子", "梯子", "桶", "铲子", "水壶",
    "眼睛", "鼻子", "嘴巴", "耳朵", "手", "脚", "尾巴", "翅膀", "牙齿", "头发",
    "眼泪", "笑容", "雨靴", "口罩", "牙刷", "毛巾", "肥皂", "被子", "枕头", "毯子",
)

# 幼儿不该在提问里遇到的抽象词（规范 §5.2）
ABSTRACT_WORDS: tuple[str, ...] = (
    "象征", "寓意", "道理", "意义", "内涵", "主题", "暗示", "启示", "含义",
    "概念", "价值", "本质", "隐喻", "深层", "表达", "分析", "评价", "总结",
    "心情", "情绪", "感受", "想法", "愿望", "性格", "友谊",
)

# 推测性表达：只能出现在 speculative_flags，不能直接写进忠实描述
SPECULATIVE_WORDS: tuple[str, ...] = (
    "可能", "也许", "大概", "似乎", "好像", "估计", "应该会", "推测", "想必", "说不定",
)

# 封闭式问句（孩子只需回答"是/对"）
CLOSED_PATTERN = re.compile(r"是不是|对不对|好不好|有没有|会不会|要不要|行不行|吗\s*[？?]|吧\s*[？?]")

# 无实义的高频字，避免把"这个""一下"当成实体
FILLER_WORDS: tuple[str, ...] = (
    "什么", "怎么", "为什么", "哪里", "哪个", "谁", "一起", "我们", "你们", "他们",
    "这个", "那个", "这里", "那里", "现在", "刚才", "接下来", "觉得", "看看", "说说",
    "小朋友", "孩子", "老师", "一下", "一个", "可以", "知道", "不知道", "开心", "难过",
)

# 合规红线：教学建议/周报里出现这些词即判失败（不排名、不贴标签）
RANKING_WORDS: tuple[str, ...] = (
    "排名", "第几名", "倒数", "比谁", "比其他", "比同班", "强于", "落后", "优秀于",
    "能力强", "能力弱", "智力", "天赋", "笨", "聪明", "落后于", "不如",
)

_SORTED_NOUNS = sorted(PICTURE_NOUNS, key=len, reverse=True)


def find_nouns(text: str) -> list[str]:
    """最长匹配抽取画面名词，返回去重后按出现顺序排列的列表。"""
    if not text:
        return []
    found: list[str] = []
    index = 0
    while index < len(text):
        for noun in _SORTED_NOUNS:
            if text.startswith(noun, index):
                if noun not in found:
                    found.append(noun)
                index += len(noun)
                break
        else:
            index += 1
    return found


def find_abstract(text: str) -> list[str]:
    return [word for word in ABSTRACT_WORDS if word in (text or "")]


def find_speculative(text: str) -> list[str]:
    return [word for word in SPECULATIVE_WORDS if word in (text or "")]


def find_ranking(text: str) -> list[str]:
    return [word for word in RANKING_WORDS if word in (text or "")]


def is_closed_question(text: str) -> bool:
    return bool(CLOSED_PATTERN.search(text or ""))


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


def latin_ratio(text: str) -> float:
    if not text:
        return 0.0
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    letters = sum(1 for ch in text if ch.isalpha())
    return latin / letters if letters else 0.0