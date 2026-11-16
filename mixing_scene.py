from manim import *
import os
import textwrap

class MixingGraph(Scene):
    def construct(self):
        # 1. Read inputs from FastAPI
        text = os.getenv("SCENE_TEXT", "Mixing technique")
        video_type = os.getenv("VIDEO_TYPE", "short")
        x_pct = float(os.getenv("TEXT_X_PCT", "0.45"))
        y_pct = float(os.getenv("TEXT_Y_PCT", "0.82"))
        total_duration = float(os.getenv("AUDIO_DURATION", "4.5"))

        # 2. Set clean white screen background
        self.camera.background_color = WHITE

        # 3. Configure layout and YouTube safe zones for Shorts (9:16) vs Long-form (16:9)
        if video_type == "short":
            x_len, y_len = 5.8, 7.2
            wrap_chars = 24
            safe_x_min, safe_x_max = 0.06, 0.84
            safe_y_min, safe_y_max = 0.24, 0.88
            graph_shift = DOWN * 0.6
        else:
            x_len, y_len = 9.2, 4.8
            wrap_chars = 52
            safe_x_min, safe_x_max = 0.05, 0.95
            safe_y_min, safe_y_max = 0.12, 0.93
            graph_shift = DOWN * 0.5

        # 4. Live parameters for flexible cursor and curve adjustments
        cursor_x = ValueTracker(0)
        curve_power = ValueTracker(1)
        curve_gain = ValueTracker(1)

        ax = Axes(
            x_range=[0, 5],
            y_range=[0, 25],
            x_length=x_len,
            y_length=y_len,
            axis_config={"color": BLACK}
        ).shift(graph_shift)

        def get_y(x):
            return curve_gain.get_value() * (x ** curve_power.get_value())

        graph = always_redraw(
            lambda: ax.plot(get_y, x_range=[0, 5], color=BLUE_D, stroke_width=4)
        )

        cursor = always_redraw(
            lambda: Dot(color=RED_D).scale(1.3).move_to(
                ax.c2p(cursor_x.get_value(), get_y(cursor_x.get_value()))
            )
        )

        # 5. Build wrapped dark text overlay with subtle light card backdrop
        frame_w = config.frame_width
        frame_h = config.frame_height
        max_safe_width = frame_w * (safe_x_max - safe_x_min)

        wrapped_text = "\n".join(textwrap.wrap(text, width=wrap_chars))
        raw_text = Text(wrapped_text, font_size=34, color=BLACK, line_spacing=1.1)
        if raw_text.width > max_safe_width - 0.4:
            raw_text.scale_to_fit_width(max_safe_width - 0.4)

        bg_box = SurroundingRectangle(
            raw_text,
            color=GRAY_C,
            fill_color=GRAY_A,
            fill_opacity=0.85,
            buff=0.22,
            corner_radius=0.15
        )
        text_overlay = VGroup(bg_box, raw_text)

        # 6. Position text at your chosen (x_pct, y_pct) clamped inside YouTube safe zones
        half_w = text_overlay.width / 2
        half_h = text_overlay.height / 2

        min_x = (-frame_w / 2) + (frame_w * safe_x_min) + half_w
        max_x = (-frame_w / 2) + (frame_w * safe_x_max) - half_w
        min_y = (-frame_h / 2) + (frame_h * safe_y_min) + half_h
        max_y = (-frame_h / 2) + (frame_h * safe_y_max) - half_h

        target_x = (-frame_w / 2) + (frame_w * x_pct)
        target_y = (-frame_h / 2) + (frame_h * y_pct)

        final_x = max(min_x, min(max_x, target_x))
        final_y = max(min_y, min(max_y, target_y))
        text_overlay.move_to([final_x, final_y, 0])

        # 7. Scale animation timing to match the full voiceover length
        part1_time = total_duration * 0.35
        part2_time = total_duration * 0.45
        hold_time = total_duration * 0.20

        self.add(ax, graph, cursor, text_overlay)
        self.play(cursor_x.animate.set_value(3), run_time=part1_time)
        self.play(
            curve_power.animate.set_value(2),
            curve_gain.animate.set_value(0.8),
            cursor_x.animate.set_value(5),
            run_time=part2_time
        )
        self.wait(hold_time)
