import main_multiuser as mm


class _DummyUserCtx:
    def __init__(self, gambling_fund):
        self._runtime = {
            "gambling_fund": gambling_fund,
            "account_balance": 0,
        }

    def set_runtime(self, key, value):
        self._runtime[key] = value

    def get_runtime(self, key, default=None):
        return self._runtime.get(key, default)


def test_startup_balance_snapshot_keeps_manual_gambling_fund():
    user_ctx = _DummyUserCtx(gambling_fund=1234567)

    fund = mm._apply_startup_balance_snapshot(user_ctx, balance=99999999)

    assert user_ctx.get_runtime("account_balance") == 99999999
    assert user_ctx.get_runtime("gambling_fund") == 1234567
    assert fund == 1234567


def test_startup_balance_snapshot_normalizes_invalid_gambling_fund():
    user_ctx = _DummyUserCtx(gambling_fund="not-a-number")

    fund = mm._apply_startup_balance_snapshot(user_ctx, balance=5000000)

    assert user_ctx.get_runtime("account_balance") == 5000000
    assert fund == 0

