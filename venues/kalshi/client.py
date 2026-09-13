                metadata={
                    "event_ticker": item.get("event_ticker"),
                    "event_title": item.get("event_title"),
                    "resolution_text": " ".join(
                        str(item.get(field, ""))
                        for field in ("rules_primary", "rules_secondary")
                        if item.get(field)
                    ),
                    "raw": item,
                },
            )
