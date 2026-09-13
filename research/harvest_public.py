                if isinstance(event, dict):
                    events[str(event.get("id") or event.get("slug"))] = event

        searched: dict[str, dict[str, Any]] = {}
        for query in (
            "Federal Reserve",
            "US CPI inflation",
            "CPI July 2026",
            "CPI June 2026",
            "unemployment payrolls",
            "nonfarm payrolls",
        ):
            try:
                payload = await self._get(
                    f"{GAMMA_ROOT}/public-search",
            raw_events = payload.get("events", []) if isinstance(payload, dict) else []
            for event in raw_events:
                if isinstance(event, dict):
                    searched[str(event.get("id") or event.get("slug"))] = event
        return [
            *searched.values(),
            *(event for key, event in events.items() if key not in searched),
        ]

    @staticmethod
    def _text(value: dict[str, Any]) -> str:
