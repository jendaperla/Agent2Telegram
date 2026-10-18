"""Small stdlib-only parameterization: each case is a separately collected test."""
from functools import wraps
from itertools import product


def cases(**parameters):
    def mark(method):
        method._cases = parameters
        return method
    return mark


def expand_cases(cls):
    for name, method in list(vars(cls).items()):
        parameters = getattr(method, '_cases', None)
        if parameters is None:
            continue
        combinations = list(product(*parameters.values()))
        if not combinations:
            raise ValueError(f'{name}: empty test cases')
        delattr(cls, name)
        for index, values in enumerate(combinations):
            arguments = dict(zip(parameters, values))

            @wraps(method)
            def test(self, _method=method, _arguments=arguments):
                _method(self, **_arguments)

            case_name = f'{name}_{index:02d}'
            if hasattr(cls, case_name):
                raise ValueError(f'duplicate test name: {case_name}')
            test.__name__ = case_name
            test.__doc__ = f'{name}: {arguments!r}'
            setattr(cls, case_name, test)
    return cls
