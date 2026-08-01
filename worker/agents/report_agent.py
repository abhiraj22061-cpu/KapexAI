from langchain_mistralai import ChatMistralAI

from worker.prompts.report import REPORT_TEMPLATE


class ReportAgent:
    def __init__(self) -> None:
        self.llm = ChatMistralAI(model="mistral-small-2506", temperature=0)

    def run(self, result: str) -> str:
        chain = REPORT_TEMPLATE | self.llm
        response = chain.invoke({"result": result})
        return response.content
