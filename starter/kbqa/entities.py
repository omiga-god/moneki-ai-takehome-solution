"""实体识别：门店、商品、指标、意图词。门店名和商品名来自数据库。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .aliases import AliasTable
from .tokenizer import normalise

#: 指标的说法 -> 指标字段。这是语言词表，不是答案。
METRIC_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("refund_amount", ("退款金额", "退了多少钱", "退款额", "退款总额", "退款多少")),
    ("aov", ("客单价", "平均每单", "单均", "人均消费")),
    ("orders", ("订单数", "多少单", "单量", "订单量", "成交单数", "有效订单")),
    ("qty", ("销量", "卖了多少份", "多少份", "多少杯", "多少碗", "多少件", "卖出", "售出", "销售数量")),
    ("net_revenue", ("净营业额", "营业额", "销售额", "营收", "收入", "流水", "卖了多少钱", "业绩", "GMV")),
]

PAYMENT_WORDS = ("支付方式", "支付占比", "现金", "微信", "支付宝", "会员储值", "储值支付", "银行卡", "刷卡", "扫码")
RANK_WORDS = ("最高", "最多", "最好", "第一", "top", "排名", "最畅销", "卖得最好", "最低", "最少")
CATEGORY_WORDS = ("品类", "类别", "分类", "什么类型的店", "哪类")
STORE_WORDS = ("门店", "哪家店", "哪个店", "各店", "每家店", "分店")
PRODUCT_RANK_WORDS = ("商品", "产品", "单品", "菜品")
#: “为什么”的各种说法。与问句焦点用同一张表，免得两处不一致。
WHY_WORDS = (
    "为什么", "为何", "原因", "什么原因", "怎么回事", "咋回事", "什么情况", "怎么会",
    "凭什么", "因为什么", "出了什么问题", "出什么问题", "怎么搞的",
)
TARGET_WORDS = ("达标", "达到目标", "目标", "完成率", "有没有完成", "完成了吗")
PRICE_WORDS = ("卖多少钱", "价格", "售价", "多少钱一", "单价", "调价", "涨价", "现价")
TREND_WORDS = ("涨", "跌", "变化", "趋势", "环比", "同比", "相比", "对比", "比起", "差了", "差多少", "相差", "高了还是", "低了还是", "多多少", "少多少")
DAILY_WORDS = ("每天", "逐日", "按天", "日趋势", "每日")
#: 问的是“规定怎么写”而不是“数字是多少”：这类问题一律走知识库。
POLICY_WORDS = (
    "怎么算", "怎么计算", "口径", "定义", "算不算", "计入", "怎么处理", "规定", "政策", "制度",
    "要求", "标准", "流程", "怎么办", "怎么开", "多久", "几点", "营业时间", "审批", "可以吗",
    "能不能", "是否可以", "有哪些", "含哪些", "含有", "过敏原", "是谁", "在哪", "什么时候恢复",
    "什么规则", "怎么说", "按哪版", "依据", "算在", "算进", "包含吗", "含不含", "要不要",
)
#: 问“最近生意怎么样”这类整体经营情况：有时间范围时按数据回答。
BUSINESS_WORDS = ("经营", "生意", "业绩", "整体情况", "情况怎么样", "表现", "大盘")
#: 异常信号：不带“为什么”也是在问“这怎么回事”。
ABNORMAL_WORDS = (
    "这么低", "这么少", "太低", "偏低", "很低", "低得离谱", "掉得厉害",
    "没有营业额", "一单都没有", "一单没有", "没有订单", "为零", "是零", "都是 0", "都是0",
    "异常", "不正常", "数据丢了", "丢数据", "是不是漏了", "空的",
    # 这些说法本身就是“出了点什么事”的信号：配上门店/时间就该走异常解释。
    "怎么回事", "咋回事", "什么情况", "出了什么问题", "出什么问题", "怎么搞的",
)
#: 系统既查不到也算不出的事：天气与预测，以及公司外部的价格。
#: 这只是“可疑话题”，不能单凭它拒答——知识库确实讲这件事时一律照答（见 out_of_scope）。
#: “温度”不在这里：中心温度该不该拒收，食品安全 SOP 里写得很清楚。
CANNOT_KNOW = (
    "天气", "下雨", "降雨", "晴天", "阴天", "预报", "预测", "会不会下",
    "股票", "股价", "汇率", "房租", "房价", "工资", "薪资", "年终奖", "提成", "竞品", "对手",
)
#: 知识库确实讲这件事的门槛：词表覆盖率与检索最高分同时达到，才算“讲了”。
#: 达标就一律照答——这是防止“可疑话题词”误伤可答问题的那道闸。
MEANINGFUL_COVERAGE = 0.45
STRONG_RETRIEVAL = 20.0
#: 卖得好不好本身就是在问销量/营业额，属于数据库能算的事。
SALES_RANK_WORDS = ("卖得最好", "最畅销", "卖得最多", "销量最高", "最好卖", "卖得最差", "卖得最少", "销冠")
#: 问“名字/别名”本身时，别名词典才是答案。
NAME_WORDS = ("别名", "叫法", "叫什么", "又叫", "是不是一个", "同一个", "写法", "学名", "全称")

#: 问的是“过去那一版”的规定：这时必须把已废止的文档放回检索范围。
HISTORICAL_WORDS = (
    "旧口径", "老口径", "旧版", "老版", "上一版", "以前", "过去", "原来", "从前", "早先",
    "之前的规定", "之前怎么", "当时的规定", "当时怎么", "废止", "作废", "被取代", "老政策", "旧政策",
)

#: 期望的答案类型，用来在一篇文档里挑对句子（跨语言时几乎是唯一可用的线索）。
MONEY_QUESTION = ("多少钱", "金额", "赔", "赔付", "费用", "价格", "多少元", "花了多少", "收多少", "活动价", "售价")
CLOCK_QUESTION = ("几点", "什么时候", "哪天", "日期", "到几号", "几号")
DURATION_QUESTION = ("多久", "多长时间", "几天内", "多少天", "几小时", "多少小时")
COUNT_QUESTION = ("多少份", "多少条", "多少杯", "多少家", "几家", "多少单", "多少人", "几次", "几天",
                  "几折", "多少分", "几个", "算一次", "记一次", "算几次")
#: 兜底焦点：问了一个“量”，但没说是钱还是件数——答案句里至少要有个数字。
VALUE_QUESTION = ("多少", "几", "多大", "多高", "多长")
REASON_QUESTION = WHY_WORDS
RULE_QUESTION = ("怎么算", "怎么计算", "口径", "算不算", "计入", "怎么统计", "算法", "怎么处理", "定义")
ENTITY_QUESTION = ("哪个商品", "什么商品", "哪款", "哪个单品", "哪个产品", "哪家店", "哪个门店", "是谁", "谁负责")

#: 破坏性与越权请求：直接拒答，连数据库都不碰。
#: 按“动词类 × 数据对象”判断意图，而不是背一串句子：
#: 删、清、改、插、补、伪造……只要落在数据对象上，无论说得多客气都是写操作。
WRITE_VERBS = (
    "删除", "删掉", "删了", "删一下", "清空", "清除", "清掉", "抹掉", "抹平", "去掉", "移除",
    "干掉", "改掉", "改成", "改为", "改一下", "修改", "调整", "篡改", "覆盖", "替换", "更新",
    "插入", "新增", "添加", "加一条", "加几条", "补一条", "补几条", "补录", "补上", "补进去",
    "录入", "写入", "伪造", "编造", "造几条", "调高", "调低", "重算", "重置", "回滚", "导入",
)
DATA_OBJECTS = (
    "数据", "数据库", "记录", "明细", "流水", "订单", "销售", "销量", "表", "行", "字段",
    "数字", "金额", "营业额", "客单价", "退款", "库存", "sales", "pos", "db",
)
#: 看起来像动词但其实是业务名词的说法，不应触发拒答（“调价通知”“数据质量”）。
_WRITE_EXCEPTIONS = re.compile(r"(调价|调休|调班|调整期|数据质量|数据来源|口径)")
_SQL_WRITE = re.compile(
    r"\b(drop|delete|truncate|alter|update|insert|replace|create|grant)\b", re.I
)
_RUN_SQL = re.compile(r"(执行|跑一下|运行|帮我跑).{0,8}(sql|语句|脚本|命令)", re.I)
#: 动词与对象之间允许隔多远。中文动宾可以颠倒，两个方向都要看。
_WRITE_WINDOW = 16

PROBE_WORDS = ("系统提示词", "提示词", "system prompt", "你的指令", "你的规则", "内部提示",
               "表结构", "schema", "数据库结构", "建表语句", "所有表名", "字段列表")
PROMPT_PROBE = (
    re.compile(r"(忽略|无视|绕过).{0,8}(规则|指令|设定|限制)"),
)

FOLLOW_UP = (
    re.compile(r"^(那|那么|接着|然后)"),
    re.compile(r"(呢|如何|怎么样)[？?]?$"),
    re.compile(r"^(它|他们|这家|那家|这个|那个|同期|同比)"),
)


@dataclass
class Catalog:
    """数据库里的门店与商品，外加知识库的别名表。"""

    stores: list[dict] = field(default_factory=list)
    products: list[dict] = field(default_factory=list)
    aliases: Optional[AliasTable] = None

    def store_ids(self) -> list[str]:
        return [store["store_id"] for store in self.stores]

    def store_name(self, store_id: str) -> str:
        for store in self.stores:
            if store["store_id"] == store_id:
                return store["store_name"]
        return store_id

    def product_name(self, product_id: str) -> str:
        for product in self.products:
            if product["product_id"] == product_id:
                return product["product_name"]
        return product_id

    def product_price(self, product_id: str) -> Optional[float]:
        for product in self.products:
            if product["product_id"] == product_id:
                return product["unit_price"]
        return None

    # -- 解析 -------------------------------------------------------------------

    def find_store(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """返回 (store_id, 未知门店编号)。问到不存在的门店时第二项非空。"""
        lowered = normalise(text)
        for code in re.findall(r"\bs\d{1,2}\b", lowered):
            upper = code.upper()
            if upper in self.store_ids():
                return upper, None
            return None, upper
        for store in self.stores:
            if normalise(store["store_name"]) in lowered:
                return store["store_id"], None
        if self.aliases:
            # 实体归一只认整词：宁可认不出，也不能把商品认成门店。
            for canonical in self.aliases.strict_mentions(text):
                code = self.aliases.store_code_of.get(canonical)
                if code and code in self.store_ids():
                    return code, None
                for store in self.stores:
                    if normalise(store["store_name"]) == normalise(canonical):
                        return store["store_id"], None
        return None, None

    def find_product(self, text: str) -> tuple[Optional[str], Optional[str]]:
        lowered = normalise(text)
        for code in re.findall(r"\bp\d{1,2}\b", lowered):
            upper = code.upper()
            if upper in {product["product_id"] for product in self.products}:
                return upper, None
            return None, upper
        matches = [
            product
            for product in self.products
            if normalise(product["product_name"]) in lowered
        ]
        if matches:
            best = max(matches, key=lambda product: len(product["product_name"]))
            return best["product_id"], None
        if self.aliases:
            for canonical in self.aliases.strict_mentions(text):
                for product in self.products:
                    if normalise(product["product_name"]) == normalise(canonical):
                        return product["product_id"], None
        return None, None


def find_metric(text: str) -> Optional[str]:
    for metric, words in METRIC_WORDS:
        if any(word in text for word in words):
            return metric
    return None


def has_any(text: str, words) -> bool:
    lowered = normalise(text)
    return any(normalise(word) in lowered for word in words)


def is_destructive(text: str) -> bool:
    """写操作意图 = 动词类 × 数据对象，两者靠得足够近就算。

    “把 S01 的销售记录删掉”“帮我补几条 7 月的销售数据”“给 8 月的营业额加 500”
    都会命中；“调价通知说了什么”“数据质量怎么样”不会。
    """
    lowered = normalise(text)
    if _RUN_SQL.search(lowered):
        return True
    if _SQL_WRITE.search(lowered) and any(obj in lowered for obj in DATA_OBJECTS + ("table", "from")):
        return True
    for verb in WRITE_VERBS:
        start = 0
        while True:
            position = lowered.find(verb, start)
            if position < 0:
                break
            start = position + 1
            window = lowered[
                max(0, position - _WRITE_WINDOW) : position + len(verb) + _WRITE_WINDOW
            ]
            if _WRITE_EXCEPTIONS.search(window):
                continue
            if any(obj in window for obj in DATA_OBJECTS):
                return True
    return False


def is_prompt_probe(text: str) -> bool:
    lowered = normalise(text)
    if any(normalise(word) in lowered for word in PROBE_WORDS):
        return True
    return any(pattern.search(text) for pattern in PROMPT_PROBE)


#: 问句的“焦点”：答案应该长什么样。一个问题可以同时有多个焦点
#: （“做活动的是哪个商品，活动价多少”既要商品名又要金额）。
FOCUS_WORDS = (
    ("reason", REASON_QUESTION),
    ("rule", RULE_QUESTION),
    ("money", MONEY_QUESTION),
    ("duration", DURATION_QUESTION),
    ("clock", CLOCK_QUESTION),
    ("count", COUNT_QUESTION),
    ("entity", ENTITY_QUESTION),
    ("value", VALUE_QUESTION),
)


def focus_kinds(text: str) -> list[str]:
    """这句话在问什么形状的答案。按出现顺序返回，可能有多个。

    `value` 是兜底：只有在说不清问的是钱还是件数时才用它（“送多少”），
    一旦有更具体的焦点，就不要再用“句子里有数字”这种宽泛条件稀释它。
    """
    kinds = [kind for kind, words in FOCUS_WORDS if has_any(text, words)]
    specific = [kind for kind in kinds if kind != "value"]
    return specific or kinds


def expected_value_kind(text: str) -> Optional[str]:
    kinds = focus_kinds(text)
    return kinds[0] if kinds else None


def wants_historical(text: str) -> bool:
    """问的是“以前那一版”吗。"""
    return has_any(text, HISTORICAL_WORDS)


def is_abnormal(text: str) -> bool:
    """不带“为什么”，但确实在问“这怎么回事”。"""
    return has_any(text, ABNORMAL_WORDS)


def asks_about_names(text: str) -> bool:
    return has_any(text, NAME_WORDS)


def head_clause(text: str) -> str:
    """主句：第一个分句。“下周天气怎么样，要不要多备货”的主语是天气，
    后面挂一句业务话也不会让它变成一个能回答的问题。"""
    return re.split(r"[，,。；;？?！!]", (text or "").strip(), maxsplit=1)[0]


def out_of_scope(text: str, coverage: float, top_score: float) -> Optional[str]:
    """主句问的是系统无从知道的事吗。返回拒答原因；None 表示可以正常回答。

    两步，顺序很重要：
    1. 先问语料：主句的词表覆盖率与检索最高分都达标，说明知识库确实讲这件事，
       一律照答——哪怕句子里出现“温度”“天气”这种词。
       “到货中心温度多少度就该拒收”写在食品安全 SOP 里，拒答它比答错更糟。
    2. 语料没讲，而主句又点到天气、预测、外部价格、薪酬这类我们无从观察的事 → 拒答；
       句尾挂一句“要不要多备货”不改变结论，因为主句才是问题本身。

    “主句里什么都查不到”这一类（附近的电影院、股票代码）不在这里判：
    它由作答阶段的检索闸门统一处理，那里有检索结果可看，判得更准。
    """
    if coverage >= MEANINGFUL_COVERAGE and top_score >= STRONG_RETRIEVAL:
        return None
    if has_any(head_clause(text), CANNOT_KNOW):
        return "主句问的是天气、预测、外部价格、薪酬这类系统无从知道的事，知识库里也没有找到相应的规定"
    return None


def looks_like_follow_up(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) <= 12 and any(pattern.search(stripped) for pattern in FOLLOW_UP):
        return True
    return bool(FOLLOW_UP[0].search(stripped) and len(stripped) <= 20)
