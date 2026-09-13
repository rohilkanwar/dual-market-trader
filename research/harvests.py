                enriched.setdefault("event_title", event.get("title", ""))
                enriched.setdefault("event_slug", event.get("slug", ""))
                nested_items.append(enriched)
        unique: dict[str, JsonObject] = {}
        for index, market in enumerate((*nested_items, *self.polymarket_cpi_resolved)):
            key = str(
                market.get("conditionId")
                or market.get("condition_id")
                or market.get("id")
                or market.get("slug")
                or index
            )
            unique[key] = market
        return tuple(unique.values())


def _read_json(path: Path, default: Any, warnings: list[str]) -> Any:
