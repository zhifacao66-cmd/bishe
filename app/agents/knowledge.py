"""知识库 Agent（RAG）：先从本地知识库检索，再让本地模型基于资料作答。

设计要点：
- 检索与生成分离：检索是确定性的（BM25），模型只负责“读资料说话”，
  这样即使模型幻觉也能通过引用回溯核对，符合政务场景的可核查要求。
- 答不出来就说答不出来：资料不足时明确提示“知识库无相关条款”，不编造。
- 检索本身只花几毫秒，问答才调模型，避免拖慢 FDE 主链路。
"""
from app.agents.base import BaseAgent
from app.kb.store import get_index
from app.llm.client import LLMUnavailable, chat

SYSTEM = """你是社区工作者的政策助手，服务对象是一线网格员和社区干事。

【回答规则】
1. 只依据【资料】回答，不得使用资料之外的知识，不得编造条款、金额、时限；
2. 每一条结论后面用《文件名》标注出处；
3. 资料不足以回答时，直接说“知识库中未收录相关规定，建议向街道对应条线确认”，不要猜测；
4. 用简洁的中文分点回答，先给结论，再给办理步骤或注意事项；
5. 涉及居民个人信息时，提醒按最小必要原则处理。"""


class KnowledgeAgent(BaseAgent):
    name = "knowledge"

    # ---------------------------------------------------------- 仅检索
    def run(self, payload: dict) -> dict:
        query = (payload.get("query") or "").strip()
        top_k = int(payload.get("top_k") or 4)
        hits = get_index().search(query, top_k) if query else []
        if query:
            self._audit("search", query,
                        f"命中 {len(hits)} 条：" + "、".join(h["title"] for h in hits[:3]))
        return {"query": query, "hits": hits}

    # ---------------------------------------------------------- 检索 + 生成
    def answer(self, question: str, top_k: int = 4) -> dict:
        hits = get_index().search(question, top_k)
        if not hits:
            return {
                "answer": "知识库中未收录与该问题相关的条款，建议向街道对应业务条线确认，"
                          "或先把相关文件上传到知识库。",
                "hits": [], "llm_used": False,
            }
        context = "\n\n".join(
            f"【资料{i + 1}】《{h['title']}》· {h['heading'] or '正文'}\n{h['text']}"
            for i, h in enumerate(hits)
        )
        prompt = (f"【资料】\n{context}\n\n"
                  f"【问题】{question}\n\n"
                  f"请依据上述资料回答，标注出处；资料没有的内容不要补充。")
        try:
            text = chat(prompt, system=SYSTEM)
            llm_used, err = True, ""
        except LLMUnavailable as e:
            # 模型不可用时降级为原文摘录，保证功能不中断。
            # 降级不是"功能正常"：必须让使用者一眼看出这是原文而不是答案，
            # 否则社工会把没经过归纳的条文当结论转发给居民。
            text = ("（模型服务暂不可用，以下为知识库原文摘录，供人工核对）\n\n"
                    + "\n\n".join(f"《{h['title']}》{h['heading']}\n{h['text']}" for h in hits[:2]))
            llm_used, err = False, str(e)
        self._audit("answer", question, text[:240])
        return {"answer": text, "hits": hits, "llm_used": llm_used, "llm_error": err}
