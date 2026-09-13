                "executable_edge": str(executable_edge),
                "fee_buffer_per_contract": str(self.parameters.fee_buffer_per_contract),
                "hedge_assumption": "buy_complementary_outcome",
                "price_signal_status": "unvalidated",
            }
            orders = (
                Order(
