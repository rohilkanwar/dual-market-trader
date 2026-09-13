
def _bucket_relation(left: JsonObject, right: JsonObject) -> str:
    left_interval, right_interval = _interval(left), _interval(right)
    if left_interval is not None and _point_value(right) is not None:
        return "kalshi_cumulative:polymarket_point"
    if left_interval is None or right_interval is None:
        return "unparsed"
    if left_interval == right_interval:
