        else:
            response = await self._http.get(
                f"{GAMMA_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "order": "liquidityNum",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            markets = []
                        ),
                        yes_token_id=token_ids[0] if token_ids else None,
                        no_token_id=token_ids[1] if len(token_ids) > 1 else None,
                        metadata={
                            "slug": item.get("slug"),
                            "resolution_text": item.get("description", ""),
                            "raw": item,
                        },
                    )
                )
        self._market_cache.update({market.market_id: market for market in markets})
