"""Shared test fixtures."""

from relayer.config import Channel, Heteronym


def heteronym(name="pa", tier="standard", enabled=True, folder=None, allow=("Read",)):
    return Heteronym(name=name, template="assistant", tier=tier,
                   channel=Channel("signal", f"group.{name}"),
                   folders=(folder or f"/work/{name}",), allow=tuple(allow),
                   deny=("Edit(./.claude/**)",), idle_stop=None, enabled=enabled)


class FakeBackend:
    """Records calls; ``fail`` names sessions whose start raises."""

    def __init__(self, running=None, fail=()):
        self.running = dict(running or {})
        self.calls = []
        self.fail = set(fail)

    def list(self):
        return dict(self.running)

    def start(self, name, command, workdir, config_hash):
        self.calls.append(("start", name, command, workdir, config_hash))
        if name in self.fail:
            raise RuntimeError("boom")

    def stop(self, name):
        self.calls.append(("stop", name))
