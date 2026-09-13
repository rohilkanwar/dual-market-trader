        *,
        venue_parameters: dict[Venue, FairValueParameters] | None = None,
    ) -> None:
        self.priors = DEFAULT_PRIORS if priors is None else priors
        self.venue_parameters = (
            DEFAULT_VENUE_PARAMETERS if venue_parameters is None else venue_parameters
        )
        if any(not Decimal("0") <= prior <= ONE for prior in self.priors.values()):
            raise ValueError("all priors must be probabilities between 0 and 1")

