import functools
import inspect
import io
import threading
import typing as tp
from abc import ABCMeta
from dataclasses import dataclass

import jax
import jax.numpy as jnp
import jax.tree_util
import numpy as np
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from treex import types

A = tp.TypeVar("A")
B = tp.TypeVar("B")
T = tp.TypeVar("T", bound="TreeObject")

PAD = r"{pad}"


@dataclass
class _Context(threading.local):
    add_field_info: bool = False
    map_f: tp.Optional[tp.Callable[["TreeObject"], "TreeObject"]] = None
    map_inplace: bool = False
    call_info: tp.Optional[tp.Dict["TreeObject", tp.Tuple[types.Inputs, tp.Any]]] = None

    def __enter__(self):
        global _CONTEXT
        self._old_context = _CONTEXT
        _CONTEXT = self

    def __exit__(self, *args):
        global _CONTEXT
        _CONTEXT = self._old_context


_CONTEXT = _Context()


class FieldInfo:
    def __init__(
        self,
        name: str,
        value: tp.Any,
        annotation: tp.Type[types.TreePart],
        module: "TreeObject",
    ):
        self.name = name
        self.value = value
        self.annotation = annotation
        self.module = module


class TreeObjectMeta(ABCMeta):
    def __call__(cls, *args, **kwargs) -> "TreeObject":
        obj: TreeObject = cls.__new__(cls)

        # copy annotations before __init__ is called
        obj.__annotations__ = {
            field: _resolve_tree_type(field, value)
            for field, value in obj.__annotations__.items()
        }

        obj.__init__(*args, **kwargs)
        # obj: TreeObject = type.__call__(cls, *args, **kwargs)

        if not obj._init_called:
            raise RuntimeError(
                f"{obj.__class__.__name__} not initialized properly, constructor must call `super().__init__()`"
            )

        # auto-annotations
        for field, value in vars(obj).items():
            if field not in obj.__annotations__ and isinstance(value, TreeObject):
                obj.__annotations__[field] = type(value)

        if isinstance(obj, tp.Callable):
            new_call = _get_call(cls.__call__)
            cls.__call__ = functools.wraps(cls.__call__)(new_call)

        return obj


# override __call__
def _get_call(orig_call):
    def _meta_call(self: "TreeObject", *args, **kwargs):
        outputs = orig_call(self, *args, **kwargs)

        if _CONTEXT.call_info is not None:
            inputs = types.Inputs(*args, **kwargs)
            _CONTEXT.call_info[self] = (inputs, outputs)

        return outputs

    return _meta_call


class TreeObject(metaclass=TreeObjectMeta):
    _init_called: bool = False

    def __init__(self) -> None:
        self._init_called = True

    def tree_flatten(self):

        if _CONTEXT.map_f is not None and _CONTEXT.map_inplace:
            _CONTEXT.map_f(self)

        fields = vars(self)

        tree = {}
        not_tree = {}

        for field, value in fields.items():
            # auto-annotations
            if field not in self.__annotations__ and isinstance(value, TreeObject):
                self.__annotations__[field] = type(value)

            annotation = self.__annotations__.get(field, None)

            if annotation is None:
                not_tree[field] = value

            elif not isinstance(annotation, tp.Type):
                not_tree[field] = value

            elif issubclass(annotation, TreeObject):
                tree[field] = value

            elif issubclass(annotation, types.TreePart):
                _annotation = annotation  # help static analyzer
                if _CONTEXT.add_field_info:
                    tree[field] = jax.tree_map(
                        lambda x: FieldInfo(
                            name=field,
                            value=x,
                            annotation=_annotation,
                            module=self,
                        ),
                        value,
                    )
                else:
                    tree[field] = value
            else:
                not_tree[field] = value

        children = (tree,)

        return children, not_tree

    @classmethod
    def tree_unflatten(cls, not_tree, children):
        module = cls.__new__(cls)
        (tree,) = children

        for k, v in tree.items():
            setattr(module, k, v)

        for k, v in not_tree.items():
            setattr(module, k, v)

        if _CONTEXT.map_f is not None and not _CONTEXT.map_inplace:
            module = _CONTEXT.map_f(module)

        return module

    def __init_subclass__(cls):
        jax.tree_util.register_pytree_node_class(cls)

    def module_init(self, key: jnp.ndarray) -> None:
        pass

    def copy(self: T) -> T:
        """
        Returns a deep copy of the module, implemented as:
        ```python
        jax.tree_map(lambda x: x, self)
        ```
        """
        return jax.tree_map(lambda x: x, self)

    def __repr__(self) -> str:
        rep = _get_repr(self, level=0, array_type=None, inline=False)
        return _get_rich_repr(Text.from_markup(rep))

    def filter(
        self: T,
        *filters: tp.Union[
            tp.Type["types.TreePart"],
            tp.Callable[[FieldInfo], bool],
        ],
    ) -> T:
        """
        Creates a new module with the same structure, but sets to `Nothing` leaves that
        do not match any of the given filters. If a `TreePart` type `t` is given, the filter

        ```python
        _filter(x: Type[TreePart]) = issubclass(x, t)
        ```

        will be created for that type.

        Example:
        ```python
        class MyModule(tx.TreeObject):
            a: tx.Parameter = 1
            b: tx.BatchStat = 2

        module = MyModule()

        module.filter(tx.Parameter) # MyModule(a=1, b=Nothing)
        module.filter(tx.BatchStat) # MyModule(a=Nothing, b=2)
        ```

        More fancy filters can be created by using callables:

        ```python
        # all States that are not OptStates
        module.filter(
            lambda field: issubclass(field.annotation, tx.State)
            and not issubclass(field.annotation, tx.OptState)
        )
        # MyModule(a=Nothing, b=2)
        ```

        Arguments:
            filters: Types to filter by, membership is determined by `issubclass`, or
                callables that take in a `FieldInfo` and return a `bool`.
        Returns:
            The new module with the filtered fields.

        """

        filters = tuple(
            _get_filter(f) if isinstance(f, tp.Type) else f for f in filters
        )

        def apply_filters(info: FieldInfo) -> tp.Any:
            return info.value if any(f(info) for f in filters) else types.Nothing()

        with _Context(add_field_info=True):
            module = jax.tree_map(apply_filters, self)

        return module

    def update(self: T, other: T, *rest: T, inplace: bool = False) -> T:
        """
        Creates a new module with the same structure, but its values
        updated based on the values from the incoming modules. Updates are performed using
        the following rules:

        * Updates are performed in the order of the input modules from left to right.
        * If a leaf value from an incoming module is `Nothing`, it wont update
            the corresponding value on the currently aggregated module.
        * The static state of the output module (`initialized`, `training`, and user defined static fields)
            is the same as the current module (`self`).

        Example:

        ```python
        a = MyModule(x=Nothing, y=2, z=3)
        b = MyModule(x=1, y=Nothing, z=4)

        a.update(b) # MyModule(x=1, y=2, z=4)
        ```
        Notice the following:

        * The value of `x` and `z` were updated since they were present in `b`.
        * The value of `y` was not updated since `b.y` was `Nothing`.

        When using `update` with multiple modules the following equivalence holds:

        ```
        m1.update(m2, m3) = m1.update(m2).update(m3)
        ```

        If you want to update the current module instead of creating a new one use `inplace=True`.
        This is useful when applying transformation inside a method where reassigning `self` is not possible:

        ```python
        def double_params(self):
            # this is not doing what you expect
            self = jax.tree_map(lambda x: 2 * x, self)
        ```
        Instead do this:

        ```python
        def double_params(self):
            doubled = jax.tree_map(lambda x: 2 * x, self)
            self.update(doubled, inplace=True)
        ```

        Arguments:
            other: The first to get the values to update from.
            rest: Additional modules to perform the update in order from left to right.
            inplace: If `True`, the current module is modified with the updated values.

        Returns:
            A new module with the updated values or `None` if `inplace` is `True`.
        """
        module = module_update(self, other, *rest)

        if inplace:
            self.__dict__.update(module.__dict__)
            return self
        else:
            return module

    def tabulate(
        self,
        inputs: tp.Union[tp.Any, types.Inputs, None] = None,
        depth: int = -1,
        signature: bool = False,
        param_types: bool = True,
    ) -> str:
        """
        Returns a tabular representation of the module.

        Arguments:
            depth: The maximum depth of the representation in terms of nested Modules, -1 means no limit.
            signature: Whether to show the signature of the TreeObject.
            param_types: Whether to show the types of the parameters.
        Returns:
            A string containing the tabular representation.
        """
        self = self.copy()

        if inputs is not None:
            if not isinstance(inputs, types.Inputs):
                inputs = types.Inputs(inputs)

            if not isinstance(self, tp.Callable):
                raise TypeError(
                    "`inputs` can only be specified if the module is a callable."
                )

            with _Context(call_info={}):

                # call using self to preserve references
                def eval_call(args, kwargs):
                    assert isinstance(self, tp.Callable)
                    return self(*args, **kwargs)

                jax.eval_shape(
                    eval_call,
                    inputs.args,
                    inputs.kwargs,
                )
                call_info = _CONTEXT.call_info

        else:
            call_info = None

        with _Context(add_field_info=True):
            flat, _ = jax.tree_flatten(self)
            tree_part_types: tp.Tuple[tp.Type[types.TreePart], ...] = tuple(
                {value_annotation.annotation for value_annotation in flat}
            )

        path = ()
        rows = list(
            _get_tabulate_rows(
                path, self, depth, tree_part_types, signature, param_types
            )
        )

        modules = [row[0] for row in rows]
        rows = [row[1:] for row in rows]

        if call_info is not None:
            for module, row in zip(modules, rows):
                if module in call_info:
                    inputs, outputs = call_info[module]
                    simplified_inputs = (
                        inputs.args[0]
                        if len(inputs.kwargs) == 0 and len(inputs.args) == 1
                        else inputs.kwargs
                        if len(inputs.kwargs) == 0
                        else inputs.kwargs
                        if len(inputs.args) == 0
                        else (inputs.args, inputs.kwargs)
                    )

                    inputs_repr = _format_param_tree(simplified_inputs)
                    outputs_repr = _format_param_tree(outputs)
                else:
                    inputs_repr = ""
                    outputs_repr = ""

                row.insert(3, outputs_repr)
                row.insert(3, inputs_repr)

        n_non_treepart_cols = 2 if call_info is None else 4

        rows[0][0] = "*"
        rows.append(
            [""] * n_non_treepart_cols
            + ["Total:"]
            + [
                _format_obj_size(self.filter(tree_type), add_padding=True)
                for tree_type in tree_part_types
            ]
        )
        _add_padding(rows)

        table = Table(
            show_header=True,
            show_lines=True,
            show_footer=True,
            # box=rich.box.HORIZONTALS,
        )

        table.add_column("path")
        table.add_column("module")
        table.add_column("params")

        if call_info is not None:
            table.add_column("inputs")
            table.add_column("outputs")

        for tree_part_type in tree_part_types:
            type_name = tree_part_type.__name__
            if type_name.startswith("_"):
                type_name = type_name[1:]

            table.add_column(type_name)

        for row in rows[:-1]:
            table.add_row(*row)

        table.columns[n_non_treepart_cols].footer = Text.from_markup(
            rows[-1][n_non_treepart_cols], justify="right"
        )

        for i in range(len(tree_part_types)):
            table.columns[n_non_treepart_cols + 1 + i].footer = rows[-1][
                n_non_treepart_cols + 1 + i
            ]

        table.caption_style = "bold"
        table.caption = "\nTotal Parameters: " + _format_obj_size(
            self, add_padding=False
        )

        return _get_rich_repr(table)


# --------------------------------------------------
# functions
# --------------------------------------------------


def module_map(
    f: tp.Callable[[TreeObject], TreeObject], obj: A, inplace: bool = False
) -> A:
    """
    Applies a function to all TreeObjects in a Pytree. Function very similar to `jax.tree_map`,
    but works on modules instead of values.

    If `inplace` is `True`, the original module is mutated.

    Arguments:
        f: The function to apply to all submodules.
        obj: base pytree.
        inplace: If `True`, the original TreeObjects are mutated.

    Returns:
        A new pytree with the updated TreeObjects or the same input `obj` if `inplace` is `True`.
    """

    with _Context(map_f=f, map_inplace=inplace):
        if inplace:
            jax.tree_flatten(obj)
            return obj
        else:
            return jax.tree_map(lambda x: x, obj)


def annotation_map(
    f: tp.Callable[[tp.Type], tp.Type], obj: A, inplace: bool = False
) -> A:
    def _map_annotations(module: TreeObject) -> TreeObject:
        module.__annotations__ = module.__annotations__.copy()
        for field, annotation in module.__annotations__.items():
            if issubclass(annotation, types.TreePart):
                module.__annotations__[field] = f(annotation)

        return module

    return module_map(_map_annotations, obj, inplace=inplace)


def module_update(module: A, other: A, *rest: A) -> A:
    """
    Functional version of `Module.update`, it accepts arbitray pytree structures
    that may optionally contain `Module`s and performs the `update` logic.

    Arguments:
        module: Main pytree to update.
        other: The pytree first to get the values to update from.
        rest: Additional pytree to perform the update in order from left to right.

    Returns:
        A new pytree with the updated values.
    """
    modules = (module, other) + rest

    def merge_fn(*xs):
        acc, *xs = xs
        for x in xs:
            if not isinstance(x, types.Nothing):
                acc = x
        return acc

    module = jax.tree_map(
        merge_fn, *modules, is_leaf=lambda x: isinstance(x, types.Nothing)
    )

    return module


# --------------------------------------------------
# utils
# --------------------------------------------------


def _get_filter(
    t: tp.Type[types.TreePart],
) -> tp.Callable[[FieldInfo], bool]:
    def _filter(info: FieldInfo) -> bool:
        return isinstance(t, tp.Type) and issubclass(info.annotation, t)

    return _filter


def _get_rich_repr(table):
    f = io.StringIO()
    console = Console(file=f, force_terminal=True)
    console.print(table)

    return f.getvalue()


def _get_repr(
    obj, level: int, array_type: tp.Optional[type], inline, space="  "
) -> str:
    indent_level = space * level

    if isinstance(obj, TreeObject):

        annotations = getattr(obj, "__annotations__", {})
        (tree,), _ = obj.tree_flatten()

        params = {}
        submodules = {}

        for field, value in tree.items():
            annotation: type = annotations[field]

            if issubclass(annotation, TreeObject):
                submodules[field] = value
            elif issubclass(annotation, types.TreePart):
                params[field] = value
            else:
                raise ValueError(f"Unsupported type {annotation}")

        body = [
            indent_level
            + space
            + f"{field}: {_get_repr(value, level + 1, annotations[field], inline=True)}"
            for field, value in params.items()
        ] + [
            indent_level
            + space
            + f"{field}: {_get_repr(value, level + 1, annotations[field], inline=True)}"
            for field, value in submodules.items()
        ]

        body_str = "\n".join(body)
        end_dot = ":" if not inline else ""
        type_str = (
            f"[dim]{obj.__class__.__name__}[/]" if inline else obj.__class__.__name__
        )
        # signature_repr = _format_module_signature(obj, not_tree)

        return f"{type_str}{end_dot}\n{body_str}"

    elif isinstance(obj, tp.Mapping):
        body = [
            indent_level
            + space
            + f"{field}: {_get_repr(value, level + 1, array_type, inline=True)}"
            for field, value in obj.items()
        ]
        body_str = "\n".join(body)
        end_dot = ":" if not inline else ""
        type_str = (
            f"[dim]{obj.__class__.__name__}[/]" if inline else obj.__class__.__name__
        )
        return f"{type_str}{end_dot}\n{body_str}"
        # return f"\n{body_str}"
    elif isinstance(obj, tp.Sequence):
        body = [
            indent_level
            + space
            + f"- {_get_repr(value, level + 2, array_type, inline=False)}"
            for i, value in enumerate(obj)
        ]
        body_str = "\n".join(body)
        end_dot = ":" if not inline else ""
        type_str = (
            f"[dim]{obj.__class__.__name__}[/]" if inline else obj.__class__.__name__
        )
        return f"{type_str}{end_dot}\n{body_str}"

    elif isinstance(obj, (np.ndarray, jnp.ndarray)):
        assert array_type is not None

        type_name = array_type.__name__
        if type_name.startswith("_"):
            type_name = type_name[1:]

        shape = ", ".join(str(x) for x in obj.shape)
        return f"{type_name}([green]{shape}[/]) [dim]{obj.dtype}[/]"
    else:
        return repr(obj)


def _get_tabulate_rows(
    path,
    obj,
    level,
    tree_part_types,
    include_signature,
    include_param_type,
) -> tp.Iterable[tp.List[tp.Any]]:
    if isinstance(obj, TreeObject):
        annotations = getattr(obj, "__annotations__", {})
        (tree,), not_tree = obj.tree_flatten()

        params = {}
        submodules = {}

        for field, value in tree.items():
            annotation = annotations[field]

            if issubclass(annotation, TreeObject):
                submodules[field] = value
            elif issubclass(annotation, types.TreePart):
                params[field] = value
            else:
                raise ValueError(f"Unsupported type {annotation}")

        path_str = "".join(str(x) for x in path)
        signature = _format_module_signature(obj, not_tree) if include_signature else {}
        argumentes_str = ", ".join(
            f"{arg} = {value}" for arg, value in signature.items()
        )
        signature_str = f"{obj.__class__.__name__}({argumentes_str})"

        if len(signature_str) > 40:
            argumentes_str = ",\n  ".join(
                f"{arg} = {value}" for arg, value in signature.items()
            )
            signature_str = f"{obj.__class__.__name__}(\n  {argumentes_str}\n)"

        params_repr = {
            field: jax.tree_map(
                lambda x: _format_param(x, annotations[field], include_param_type),
                value,
                is_leaf=lambda x: isinstance(x, types.Nothing),
            )
            for field, value in params.items()
        }
        params_repr = _simplify(params_repr)
        params_str = _as_yaml_str(params_repr)

        if level == 0:
            tree_part_sizes = [
                _format_obj_size(obj.filter(t), add_padding=True)
                for t in tree_part_types
            ]
        else:
            tree_part_sizes = [
                _format_obj_size(
                    [
                        value
                        for field, value in params.items()
                        if annotations[field] is t
                    ],
                    add_padding=True,
                )
                for t in tree_part_types
            ]

        yield [obj, path_str, signature_str, params_str] + tree_part_sizes

        if level != 0:
            for field, value in submodules.items():
                yield from _get_tabulate_rows(
                    path + (f".{field}",),
                    value,
                    level - 1,
                    tree_part_types,
                    include_signature,
                    include_param_type,
                )
    elif isinstance(obj, tp.Mapping):
        for field, value in obj.items():
            if isinstance(field, str):
                field = f'"{field}"'

            yield from _get_tabulate_rows(
                path + (f"[{field}]",),
                value,
                level,
                tree_part_types,
                include_signature,
                include_param_type,
            )
    elif isinstance(obj, tp.Sequence):
        for i, value in enumerate(obj):
            yield from _get_tabulate_rows(
                path + (f"[{i}]",),
                value,
                level,
                tree_part_types,
                include_signature,
                include_param_type,
            )
    else:
        raise ValueError(f"Unsupported type {type(obj)}")


def _as_yaml_str(value) -> str:
    if hasattr(value, "__len__") and len(value) == 0:
        return ""

    file = io.StringIO()
    yaml.safe_dump(
        value,
        file,
        default_flow_style=False,
        indent=2,
        sort_keys=False,
        explicit_end=False,
    )
    return file.getvalue().replace("\n...", "").replace("'", "")


def _simplify(obj):
    if isinstance(obj, str):
        return obj
    elif isinstance(obj, tp.Sequence):
        return [_simplify(x) for x in obj]
    elif isinstance(obj, tp.Mapping):
        return {k: _simplify(v) for k, v in obj.items()}
    else:
        return obj


def _format_module_signature(
    obj: TreeObject, not_tree: tp.Dict[str, tp.Any]
) -> tp.Dict[str, str]:
    signature = {}
    simple_types = {int, str, float, bool}
    cls = obj.__class__
    values = list(inspect.signature(cls.__init__).parameters.values())[1:]
    names = {x.name for x in values}

    for field in not_tree:
        if field in names:
            value = not_tree[field]
            types = set(jax.tree_leaves(jax.tree_map(type, value)))

            if types.issubset(simple_types):
                signature[field] = value

    signature = jax.tree_map(
        lambda x: f'"{x}"'
        if isinstance(x, str)
        else f"{x:.4f}"
        if isinstance(x, float)
        else str(x),
        signature,
    )
    signature = {
        field: str(value).replace("'", "") for field, value in signature.items()
    }

    return signature


def _format_param(obj, array_type, include_param_type) -> str:

    if isinstance(obj, (np.ndarray, jnp.ndarray)):
        if include_param_type:
            type_name = array_type.__name__
            if type_name.startswith("_"):
                type_name = type_name[1:]
        else:
            type_name = ""

        shape = ", ".join(str(x) for x in obj.shape)
        return f"{type_name}([green]{shape}[/]){PAD}  [dim]{obj.dtype}[/]"
    else:
        return str(obj) if include_param_type else f"() [dim]{type(obj).__name__}[/]"


def _format_param_tree(params) -> str:

    params_repr = jax.tree_map(
        lambda x: _format_param(x, None, include_param_type=False),
        params,
        is_leaf=lambda x: isinstance(x, types.Nothing),
    )

    params_repr = _simplify(params_repr)
    params_str = _as_yaml_str(params_repr)

    return params_str


def _format_obj_size(obj, add_padding):
    count = sum((x.size if hasattr(x, "size") else 0 for x in jax.tree_leaves(obj)), 0)
    size = sum(
        (x.nbytes if hasattr(x, "nbytes") else 0 for x in jax.tree_leaves(obj)), 0
    )
    padding = f"{PAD}" if add_padding else ""

    return (
        f"[green]{count:,}[/]{padding}  [dim]{_format_size(size)}[/]"
        if count > 0
        else ""
    )


def _add_padding(rows):
    n_cols = len(rows[0])

    for col in range(n_cols):
        max_length = max(
            len(line.split(PAD)[0]) for row in rows for line in row[col].split("\n")
        )

        for row in rows:
            if PAD in row[col]:
                row[col] = "\n".join(
                    line.format(
                        pad=" " * (max_length - len(line.rstrip().split(PAD)[0]))
                    )
                    if PAD in line
                    else line
                    for line in row[col].rstrip().split("\n")
                )


def _format_size(size):
    count, units = (
        (f"{size / 1e9 :,.1f}", "GB")
        if size > 1e9
        else (f"{size / 1e6 :,.1f}", "MB")
        if size > 1e6
        else (f"{size / 1e3 :,.1f}", "KB")
        if size > 1e3
        else (f"{size:,}", "B")
    )

    return f"{count}{units}"


def _safe_issubclass(a, b) -> bool:
    return issubclass(a, b) if isinstance(a, tp.Type) else issubclass(type(a), b)


def _generic_issubclass(__cls, __class_or_tuple) -> bool:
    return _safe_issubclass(__cls, __class_or_tuple) or (
        hasattr(__cls, "__origin__")
        and _safe_issubclass(__cls.__origin__, __class_or_tuple)
    )


def _remove_generic(_cls):
    return (
        _cls
        if isinstance(_cls, type)
        else _cls.__origin__
        if hasattr(_cls, "__origin__")
        else type(_cls)
    )


def _resolve_tree_type(name: str, t: tp.Any) -> tp.Any:

    tree_types = [
        x
        for x in _all_types(t)
        if _generic_issubclass(x, (types.TreePart, TreeObject, types.Static))
    ]

    if _generic_issubclass(t, types.Static):
        if any(
            _generic_issubclass(x, (types.TreePart, types.Static))
            for x in tree_types[1:]
        ):
            raise TypeError(
                f"Generic Static annotation cannot contain TreePart or Static types, got '{name}': {t}"
            )
        return types.Static

    elif _generic_issubclass(t, types.TreePart):
        if len(tree_types) > 1:
            raise TypeError(
                f"TreePart annotations cannot contain TreeParts, TreeObject, or Static types, got '{name}': {t}"
            )
        return _remove_generic(t)
    elif any(
        _generic_issubclass(x, (types.TreePart, types.Static)) for x in tree_types[1:]
    ):
        raise TypeError(
            f"TreePart or Static annotations have to be the top-level annotation, they cannot be used inside Generic types, got '{name}': {t}"
        )

    if len(tree_types) > 1:
        # if its a type with many TreeObject subtypes just mark them all as TreeObject
        if all(_generic_issubclass(x, TreeObject) for x in tree_types):
            return TreeObject
        else:
            raise TypeError(
                f"Multiple tree parts found in annotation for field '{name}': {t}"
            )
    elif len(tree_types) == 1:
        return tree_types[0]
    else:
        return _remove_generic(t)


def _all_types(t: tp.Type) -> tp.Iterable[tp.Type]:

    if hasattr(t, "__args__"):
        yield t.__origin__

        for arg in t.__args__:
            yield from _all_types(arg)
    else:
        yield t