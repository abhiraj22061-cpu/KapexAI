class GuardrailAgent:
    def __init__(self) -> None:
        self.status = "not_implemented"

    def run(self, report: str) -> dict[str, str]:
        return {
            "status": self.status,
            "report": report,
            "message": "Guardrail agent is a placeholder. No checks implemented yet.",
        }
