"""
Web search via DuckDuckGo (no API key required).
All agents that need internet search import from here.
"""

import asyncio
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS
from core.stealth import throttle
from core.logger import get_logger

logger = get_logger("search")


class SearchClient:
    def __init__(self) -> None:
        self._ddgs = DDGS()

    async def search(
        self,
        query: str,
        max_results: int = 5,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        **_kwargs,  # absorb unused Tavily-style args (search_depth etc.)
    ) -> list[dict]:
        """
        Returns list of dicts: { url, title, content }
        """
        await throttle("search_queries")

        # Append site: filters if requested
        q = query
        if include_domains:
            q += " " + " OR ".join(f"site:{d}" for d in include_domains)
        if exclude_domains:
            q += " " + " ".join(f"-site:{d}" for d in exclude_domains)

        try:
            # DDGS.text() is synchronous — run in thread to stay async-safe
            results_raw = await asyncio.to_thread(
                self._ddgs.text, q, max_results=max_results
            )
            results = [
                {
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "content": r.get("body", ""),
                }
                for r in (results_raw or [])
            ]
            logger.info("search_complete", query=query[:80], result_count=len(results))
            return results
        except Exception as e:
            logger.error("search_failed", query=query[:80], error=str(e))
            return []

    async def search_text(self, query: str, max_results: int = 5) -> str:
        """Convenience: returns concatenated result snippets as a single string."""
        results = await self.search(query, max_results=max_results)
        return "\n\n".join(
            f"[{r.get('title', '')}] {r.get('url', '')}\n{r.get('content', '')}"
            for r in results
        )


_client: SearchClient | None = None


def get_search_client() -> SearchClient:
    global _client
    if _client is None:
        _client = SearchClient()
    return _client
