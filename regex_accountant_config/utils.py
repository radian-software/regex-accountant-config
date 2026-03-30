import typing


T = typing.TypeVar("T")


def must(obj: T | None, errmsg: str) -> T:
    assert obj is not None, errmsg
    return obj


def and_also(*funcs):
    def seq(*args, **kwargs):
        res = funcs[0](*args, **kwargs)
        for func in funcs[1:]:
            func(*args, **kwargs)
        return res

    return seq
