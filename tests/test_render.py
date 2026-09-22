"""Rendering contracts for rindicator.render_colors."""

import unittest

from rindicator import EFFECT_BAR, EFFECT_GRADIENT, render_colors


def reds(colors):
    return [color.red for color in colors]


class GradientTest(unittest.TestCase):
    def test_endpoints_and_midpoint(self):
        self.assertEqual(
            [(c.red, c.green, c.blue) for c in render_colors(0, 10, EFFECT_GRADIENT)],
            [(0, 0, 255)] * 10,
        )
        self.assertEqual(
            [(c.red, c.green, c.blue) for c in render_colors(100, 10, EFFECT_GRADIENT)],
            [(255, 0, 0)] * 10,
        )
        self.assertEqual(
            [(c.red, c.green, c.blue) for c in render_colors(50, 10, EFFECT_GRADIENT)],
            [(128, 0, 127)] * 10,
        )

    def test_clamps_percent_outside_range(self):
        self.assertEqual(reds(render_colors(-25, 4, EFFECT_GRADIENT)), [0] * 4)
        self.assertEqual(reds(render_colors(250, 4, EFFECT_GRADIENT)), [255] * 4)
        self.assertEqual(
            [c.blue for c in render_colors(250, 4, EFFECT_GRADIENT)], [0] * 4
        )


class BarTest(unittest.TestCase):
    def test_empty_and_full_for_ten_leds(self):
        self.assertEqual(
            [(c.red, c.green, c.blue) for c in render_colors(0, 10, EFFECT_BAR)],
            [(0, 0, 0)] * 10,
        )
        self.assertEqual(
            [(c.red, c.green, c.blue) for c in render_colors(100, 10, EFFECT_BAR)],
            [(255, 0, 0)] * 10,
        )

    def test_fractional_fill_for_ten_leds(self):
        colors = render_colors(25, 10, EFFECT_BAR)
        self.assertEqual(reds(colors), [255, 255, 128, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual([c.green for c in colors], [0] * 10)
        self.assertEqual([c.blue for c in colors], [0] * 10)

    def test_fill_for_six_leds(self):
        colors = render_colors(50, 6, EFFECT_BAR)
        self.assertEqual(reds(colors), [255, 255, 255, 0, 0, 0])
        self.assertEqual(reds(colors).count(255), 3)
        self.assertEqual(reds(render_colors(150, 6, EFFECT_BAR)), [255] * 6)

    def test_reverse_flips_the_finished_frame(self):
        forward = render_colors(25, 10, EFFECT_BAR)
        reversed_colors = render_colors(25, 10, EFFECT_BAR, reverse=True)
        self.assertEqual(reds(reversed_colors), reds(forward)[::-1])
        self.assertEqual(reds(reversed_colors), [0, 0, 0, 0, 0, 0, 0, 128, 255, 255])


class InvalidInputTest(unittest.TestCase):
    def test_rejects_invalid_arguments(self):
        for percent in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(percent=percent):
                with self.assertRaises(ValueError):
                    render_colors(percent, 10, EFFECT_BAR)
        for led_count in (0, -1, 1.5):
            with self.subTest(led_count=led_count):
                with self.assertRaises(ValueError):
                    render_colors(50, led_count, EFFECT_BAR)
        with self.assertRaises(ValueError):
            render_colors(50, 10, "rainbow")


if __name__ == "__main__":
    unittest.main()
