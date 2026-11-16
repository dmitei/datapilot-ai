from io import BytesIO
from threading import Lock

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from .analytics import Report


_render_lock = Lock()


def render_png(report: Report) -> bytes:
    labels, values = zip(*report.rows)
    millions = [value / 1_000_000 for value in values]
    with _render_lock:
        figure = Figure(figsize=(9, 4.5), layout="constrained")
        canvas = FigureCanvasAgg(figure)
        axes = figure.subplots()
        if report.metric.monthly:
            axes.plot(labels, millions, marker="o", color="#17665d")
            axes.set_xlabel("Месяц")
            axes.set_ylabel("млн ₽")
            axes.grid(axis="y", alpha=0.25)
        else:
            axes.barh(labels, millions, color="#17665d")
            axes.invert_yaxis()
            axes.set_xlabel("млн ₽")
            axes.grid(axis="x", alpha=0.25)
        axes.set_title(f"{report.metric.title} · {report.year}")
        with BytesIO() as buffer:
            canvas.print_png(buffer)
            return buffer.getvalue()
