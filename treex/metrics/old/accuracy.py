import typing as tp

import jax
import jax.numpy as jnp

from treex import types
from treex.metrics.mean import Mean


def accuracy(y_true: jnp.ndarray, y_pred: jnp.ndarray) -> jnp.ndarray:
    # [y_pred, y_true], _ = metrics_utils.ragged_assert_compatible_and_get_flat_values(
    #     [y_pred, y_true]
    # )
    # y_pred.shape.assert_is_compatible_with(y_true.shape)

    if y_true.dtype != y_pred.dtype:
        y_pred = y_pred.astype(y_true.dtype)

    return (y_true == y_pred).astype(jnp.float32)


class Accuracy(Mean):
    """
    Calculates how often predictions equals labels. This metric creates two local variables,
    `total` and `count` that are used to compute the frequency with which `y_pred` matches `y_true`. This frequency is
    ultimately returned as `binary accuracy`: an idempotent operation that simply
    divides `total` by `count`. If `sample_weight` is `None`, weights default to 1.
    Use `sample_weight` of 0 to mask values.

    ```python
    accuracy = elegy.metrics.Accuracy()

    result = accuracy(
        y_true=jnp.array([1, 1, 1, 1]),
        y_pred=jnp.array([0, 1, 1, 1])
    )
    assert result == 0.75  # 3 / 4

    result = accuracy(
        y_true=jnp.array([1, 1, 1, 1]),
        y_pred=jnp.array([1, 0, 0, 0])
    )
    assert result == 0.5  # 4 / 8
    ```

    Usage with elegy API:

    ```python
    model = elegy.Model(
        module_fn,
        loss=tx.losses.CategoricalCrossentropy(),
        metrics=elegy.metrics.Accuracy(),
        optimizer=optax.adam(1e-3),
    )
    ```
    """

    def __init__(
        self,
        argmax_preds: bool = False,
        argmax_labels: bool = False,
        on: tp.Optional[types.IndexLike] = None,
        name: tp.Optional[str] = None,
        dtype: tp.Optional[jnp.dtype] = None,
    ):
        self.argmax_preds = argmax_preds
        self.argmax_labels = argmax_labels
        super().__init__(on=on, name=name, dtype=dtype)

    def update(
        self,
        y_true: jnp.ndarray,
        y_pred: jnp.ndarray,
        sample_weight: tp.Optional[jnp.ndarray] = None,
    ):
        """
        Accumulates metric statistics. `y_true` and `y_pred` should have the same shape.

        Arguments:
            y_true: Ground truth values. shape = `[batch_size, d0, .. dN]`.
            y_pred: The predicted values. shape = `[batch_size, d0, .. dN]`.
            sample_weight: Optional `sample_weight` acts as a
                coefficient for the metric. If a scalar is provided, then the metric is
                simply scaled by the given value. If `sample_weight` is a tensor of size
                `[batch_size]`, then the metric for each sample of the batch is rescaled
                by the corresponding element in the `sample_weight` vector. If the shape
                of `sample_weight` is `[batch_size, d0, .. dN-1]` (or can be broadcasted
                to this shape), then each metric element of `y_pred` is scaled by the
                corresponding value of `sample_weight`. (Note on `dN-1`: all metric
                functions reduce by 1 dimension, usually the last axis (-1)).
        Returns:
            Array with the cumulative accuracy.
        """

        if self.argmax_preds:
            y_pred = jnp.argmax(y_pred, axis=-1)

        if self.argmax_labels:
            y_true = jnp.argmax(y_true, axis=-1)

        values = accuracy(y_true=y_true, y_pred=y_pred)

        super().update(
            values=values,
            sample_weight=sample_weight,
        )