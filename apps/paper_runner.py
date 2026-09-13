import argparse
import asyncio
import logging
from decimal import Decimal

from core.config import Settings
from core.execution import ExecutionEngine
from strategies import (
    CalibratedFairValueStrategy,
    CrossVenueMispricingStrategy,
    CrossVenueParameters,
    CrossVenueStrategyRunner,
    StrategyRunner,
)
        choices=("cross_venue", "fair_value", "both"),
        default="cross_venue",
    )
    parser.add_argument(
        "--cross-min-edge",
        type=Decimal,
        default=Decimal("0.04"),
        help="minimum normalized midpoint difference (default: 0.04)",
    )
    return parser.parse_args()


async def run(venue: str, strategy: str, cross_min_edge: Decimal) -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        by_venue = {client.venue: client for client in clients}
        if strategy in {"cross_venue", "both"}:
            cross_runner = CrossVenueStrategyRunner(
                CrossVenueMispricingStrategy(
                    risk=risk,
                    portfolio=portfolio,
                    parameters=CrossVenueParameters(
                        minimum_mid_edge=cross_min_edge
                    ),
                ),
                execution,
            )
            results.append(
    args = parse_args()
    if args.strategy in {"cross_venue", "both"} and args.venue != "both":
        raise SystemExit("cross_venue strategy requires --venue both")
    asyncio.run(run(args.venue, args.strategy, args.cross_min_edge))


if __name__ == "__main__":
