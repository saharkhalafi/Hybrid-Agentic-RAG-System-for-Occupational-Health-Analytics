"""Persian answer synthesis from grounded agent results."""

from __future__ import annotations

from typing import Any


class AnswerSynthesizer:
    """Template-based synthesis — no LLM invention of facts or numbers."""

    def synthesize(
        self,
        *,
        intent: str,
        query: str,
        agent_results: dict[str, Any],
        guardrail_message: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if guardrail_message:
            return guardrail_message, []

        citations: list[dict[str, Any]] = []

        if intent.startswith("CLARIFY.") or intent.startswith("GUARDRAIL."):
            return guardrail_message or "لطفاً سؤال را دقیق‌تر مطرح کنید.", citations

        hybrid = agent_results.get("hybrid")
        if hybrid and hybrid.get("agent_results"):
            return self._hybrid_answer(intent, hybrid["agent_results"], citations)

        structured = agent_results.get("structured")
        if structured and structured.get("success"):
            text, cites = self._structured_answer(intent, structured["data"])
            citations.extend(structured.get("citations") or cites)
            if agent_results.get("semantic", {}).get("success"):
                sem = agent_results["semantic"]
                extra = self._semantic_snippet(sem.get("chunks") or [])
                citations.extend(sem.get("citations") or [])
                return f"{text}\n\n{extra}", citations
            return text, citations

        formula = agent_results.get("formula")
        if formula and formula.get("success"):
            text, cites = self._formula_answer(formula["data"])
            citations.extend(formula.get("citations") or cites)
            return text, citations

        semantic = agent_results.get("semantic")
        if semantic and semantic.get("success"):
            chunks = semantic.get("chunks") or []
            citations.extend(semantic.get("citations") or [])
            if chunks:
                return self._semantic_snippet(chunks), citations
            return "اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد.", citations

        return "اطلاعات کافی برای پاسخ قطعی در داده‌های موجود پیدا نشد.", citations

    def _structured_answer(self, intent: str, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        chem = data.get("chemical_name") or data.get("english_name") or ""
        unit = data.get("unit") or "ppm"
        if intent == "STRUCTURED.CHEMICAL.BY_NAME":
            cas = data.get("cas") or ""
            fa = data.get("persian_name") or ""
            label = " ".join(part for part in (chem, fa) if part).strip() or chem
            text = f"CAS {label}: {cas}".strip() if cas else f"{label}".strip() or "داده‌ای یافت نشد."
        elif intent == "STRUCTURED.OEL.ALL_LIMITS_LOOKUP":
            parts = []
            if data.get("twa") is not None:
                parts.append(f"TWA: {data['twa']} {unit}")
            if data.get("stel") is not None:
                parts.append(f"STEL: {data['stel']} {unit}")
            if data.get("ceiling") is not None:
                parts.append(f"Ceiling: {data['ceiling']} {unit}")
            text = f"حدود مجاز مواجهه {chem}:\n" + "\n".join(parts) if parts else "داده‌ای یافت نشد."
        elif intent == "STRUCTURED.CHEMICAL.MOLECULAR_WEIGHT":
            display = data.get("molecular_weight_display") or data.get("molecular_weight")
            text = f"وزن مولکولی {chem}: {display}"
        elif intent == "STRUCTURED.OEL.PROVENANCE":
            text = f"منبع: صفحه {data.get('page_number')} — {data.get('source_row_key')}"
            extras = []
            if data.get("symbols"):
                extras.append(str(data["symbols"]))
            if data.get("health_effect"):
                extras.append(str(data["health_effect"]))
            if extras:
                text += " — " + " / ".join(extras)
        else:
            field = data.get("field") or "TWA"
            val = data.get("value")
            text = f"حد {field} برای {chem}: {val} {unit}"
            if data.get("original_value"):
                text += f" (مقدار اصلی: {data['original_value']})"
        cite = [{
            "source_type": "structured",
            "source_row_key": data.get("source_row_key"),
            "page_number": data.get("page_number"),
            "cell_id": data.get("cell_id"),
        }]
        return text, cite

    def _formula_answer(self, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if "result" in data:
            return f"نتیجه محاسبه: {data['result']}", [{"formula_id": data.get("formula_id"), "authority": "formula_engine"}]
        expr = data.get("normalized_expression") or data.get("description") or ""
        return f"فرمول: {expr}", [{"formula_id": data.get("formula_id")}]

    def _semantic_snippet(self, chunks: list[dict[str, Any]]) -> str:
        if not chunks:
            return ""
        top = chunks[0]
        content = (top.get("content") or "")[:500]
        page = top.get("page_number")
        return f"بر اساس متن مرجع (صفحه {page}):\n{content}"

    def _hybrid_answer(self, intent: str, results: dict[str, Any], citations: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        parts: list[str] = []
        if results.get("structured", {}).get("success"):
            t, c = self._structured_answer("STRUCTURED.OEL.TWA_LOOKUP", results["structured"]["data"])
            parts.append(t)
            citations.extend(c)
        if results.get("comparison"):
            comp = results["comparison"]
            parts.append(
                f"مواجهه {comp['concentration']} {comp.get('unit','ppm')} "
                f"{'بیشتر از' if comp.get('exceeds') else 'کمتر یا مساوی'} حد مجاز "
                f"(نسبت: {comp['ratio']:.2f})"
            )
        if results.get("semantic", {}).get("success"):
            parts.append(self._semantic_snippet(results["semantic"].get("chunks") or []))
            citations.extend(results["semantic"].get("citations") or [])
        if results.get("formula", {}).get("success"):
            ft, fc = self._formula_answer(results["formula"]["data"])
            parts.append(ft)
            citations.extend(fc)
        return "\n\n".join(p for p in parts if p), citations
