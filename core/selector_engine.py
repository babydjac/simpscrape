import re
from typing import Any, Dict, List, Optional

from .sitemap_loader import SelectorNode


class SelectorEngine:
    def __init__(self, dom_executor):
        self.dom = dom_executor

    def extract_page_fields(self, selectors: List[SelectorNode]) -> Dict[str, Any]:
        page_fields: Dict[str, Any] = {}
        for selector in selectors:
            if "_root" not in selector.parent_selectors:
                continue
            if selector.type == "SelectorElement":
                continue
            if selector.type == "SelectorLink":
                text_value, href_value = self._extract_link_fields(selector, context_node=None)
                page_fields[selector.id] = text_value
                page_fields[f"{selector.id}-href"] = href_value
                continue
            page_fields[selector.id] = self._extract_selector(selector, context_node=None)
        return page_fields

    def extract_records(self, selectors: List[SelectorNode], page_parent_ids: List[str]) -> List[Dict[str, Any]]:
        record_selectors = [
            s
            for s in selectors
            if s.type == "SelectorElement" and any(parent in page_parent_ids for parent in s.parent_selectors)
        ]
        default_fields = self._default_fields(selectors)
        records: List[Dict[str, Any]] = []
        for record_selector in record_selectors:
            nodes = self.dom.query_selector_all(record_selector.selector)
            if not record_selector.multiple and nodes:
                nodes = [nodes[0]]
            for node in nodes:
                record: Dict[str, Any] = dict(default_fields)
                self._extract_children(record, node, selectors, record_selector.id)
                records.append(record)
        return records

    def _extract_children(
        self,
        record: Dict[str, Any],
        context_node,
        selectors: List[SelectorNode],
        parent_id: str,
    ) -> None:
        for selector in selectors:
            if parent_id not in selector.parent_selectors:
                continue
            if selector.type == "SelectorElement":
                child_nodes = self.dom.query_selector_all(selector.selector, context=context_node)
                if not selector.multiple and child_nodes:
                    child_nodes = [child_nodes[0]]
                for child_node in child_nodes:
                    self._extract_children(record, child_node, selectors, selector.id)
                continue

            if selector.type == "SelectorLink":
                text_value, href_value = self._extract_link_fields(selector, context_node=context_node)
                record[selector.id] = text_value
                record[f"{selector.id}-href"] = href_value
            else:
                values = self._extract_selector(selector, context_node)
                record[selector.id] = values

    def _extract_selector(self, selector: SelectorNode, context_node) -> Any:
        nodes = self.dom.query_selector_all(selector.selector, context=context_node)
        if not nodes and context_node is not None:
            closest = self.dom.closest(context_node, selector.selector)
            if closest:
                nodes = [closest]
        if not selector.multiple:
            nodes = nodes[:1]

        extracted: List[Optional[str]] = []
        for node in nodes:
            raw_value = self._extract_node_value(selector, node)
            if raw_value is None and selector.type == "SelectorElementAttribute" and context_node is not None:
                closest = self.dom.closest(context_node, selector.selector)
                if closest:
                    raw_value = self._extract_node_value(selector, closest)
            cleaned = self._apply_regex(selector.regex, raw_value)
            extracted.append(cleaned)

        if selector.multiple:
            return extracted
        return extracted[0] if extracted else None

    def _extract_link_fields(self, selector: SelectorNode, context_node):
        nodes = self.dom.query_selector_all(selector.selector, context=context_node)
        if not selector.multiple:
            nodes = nodes[:1]
        texts: List[Optional[str]] = []
        hrefs: List[Optional[str]] = []
        for node in nodes:
            text = self._normalize(self.dom.get_text(node))
            href = self.dom.get_attribute(node, selector.extract_attribute or "href")
            texts.append(text)
            hrefs.append(href)
        if selector.multiple:
            return texts, hrefs
        return (texts[0] if texts else None), (hrefs[0] if hrefs else None)

    def _default_fields(self, selectors: List[SelectorNode]) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        for selector in selectors:
            if selector.type == "SelectorElement":
                continue
            if selector.type == "SelectorLink":
                fields[selector.id] = [] if selector.multiple else None
                fields[f"{selector.id}-href"] = [] if selector.multiple else None
                continue
            fields[selector.id] = [] if selector.multiple else None
        return fields

    def _extract_node_value(self, selector: SelectorNode, node) -> Optional[str]:
        if selector.type == "SelectorText":
            return self._normalize(self.dom.get_text(node))
        if selector.type == "SelectorLink":
            attr = selector.extract_attribute or "href"
            return self.dom.get_attribute(node, attr)
        if selector.type == "SelectorElementAttribute":
            attr = selector.extract_attribute or ""
            return self.dom.get_attribute(node, attr) if attr else None
        return self._normalize(self.dom.get_text(node))

    def _apply_regex(self, pattern: Optional[str], value: Optional[str]) -> Optional[str]:
        if not pattern or not value:
            return value
        if "\\n" in pattern or "\\s" in pattern:
            cleaned = re.sub(pattern, " ", value)
            return self._normalize(cleaned)
        match = re.search(pattern, value)
        return match.group(0) if match else None

    def _normalize(self, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return " ".join(value.split())
