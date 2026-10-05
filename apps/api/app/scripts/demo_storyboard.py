"""Portable raster storyboard for the original, silent synthetic demonstration.

The small bitmap alphabet and geometry are authored here. Rendering requires
Python's standard library; FFmpeg receives RGB frames without font dependencies.
"""
from __future__ import annotations

import math

WIDTH, HEIGHT, FPS = 960, 540, 24
VERSION = 'loci-synthetic-chapters-v2'
CHAPTERS = (
    {'title': 'Front face', 'shape': 'Square', 'color': '208cb8',
     'start_ms': 0, 'end_ms': 4000,
     'text': 'Section 1: Front face. A square marks the front face of the cube.'},
    {'title': 'Top edge', 'shape': 'Triangle', 'color': 'e6a03c',
     'start_ms': 4000, 'end_ms': 8000,
     'text': 'Section 2: Top edge. A triangle marks the top edge of the cube.'},
    {'title': 'Compare view', 'shape': 'Circle', 'color': '7056a0',
     'start_ms': 8000, 'end_ms': 12000,
     'text': 'Section 3: Compare view. A circle marks the side face of the cube.'},
)
BACKGROUND = (16, 27, 44)
WHITE = (248, 250, 252)
MUTED = (191, 207, 225)

# Seven rows of five pixels per glyph. Block strokes remain clear at small sizes.
_GLYPHS = {
    'A':'01110/10001/10001/11111/10001/10001/10001',
    'B':'11110/10001/10001/11110/10001/10001/11110',
    'C':'01111/10000/10000/10000/10000/10000/01111',
    'D':'11110/10001/10001/10001/10001/10001/11110',
    'E':'11111/10000/10000/11110/10000/10000/11111',
    'F':'11111/10000/10000/11110/10000/10000/10000',
    'G':'01111/10000/10000/10111/10001/10001/01111',
    'H':'10001/10001/10001/11111/10001/10001/10001',
    'I':'11111/00100/00100/00100/00100/00100/11111',
    'J':'00111/00010/00010/00010/10010/10010/01100',
    'K':'10001/10010/10100/11000/10100/10010/10001',
    'L':'10000/10000/10000/10000/10000/10000/11111',
    'M':'10001/11011/10101/10101/10001/10001/10001',
    'N':'10001/11001/10101/10011/10001/10001/10001',
    'O':'01110/10001/10001/10001/10001/10001/01110',
    'P':'11110/10001/10001/11110/10000/10000/10000',
    'Q':'01110/10001/10001/10001/10101/10010/01101',
    'R':'11110/10001/10001/11110/10100/10010/10001',
    'S':'01111/10000/10000/01110/00001/00001/11110',
    'T':'11111/00100/00100/00100/00100/00100/00100',
    'U':'10001/10001/10001/10001/10001/10001/01110',
    'V':'10001/10001/10001/10001/10001/01010/00100',
    'W':'10001/10001/10001/10101/10101/10101/01010',
    'X':'10001/10001/01010/00100/01010/10001/10001',
    'Y':'10001/10001/01010/00100/00100/00100/00100',
    'Z':'11111/00001/00010/00100/01000/10000/11111',
    '0':'01110/10001/10011/10101/11001/10001/01110',
    '1':'00100/01100/00100/00100/00100/00100/01110',
    '2':'01110/10001/00001/00010/00100/01000/11111',
    '3':'11110/00001/00001/01110/00001/00001/11110',
    '4':'00010/00110/01010/10010/11111/00010/00010',
    '5':'11111/10000/10000/11110/00001/00001/11110',
    '6':'01110/10000/10000/11110/10001/10001/01110',
    '7':'11111/00001/00010/00100/01000/01000/01000',
    '8':'01110/10001/10001/01110/10001/10001/01110',
    '9':'01110/10001/10001/01111/00001/00001/01110',
    ':':'00000/00100/00100/00000/00100/00100/00000',
    '.':'00000/00000/00000/00000/00000/00100/00100',
    '-':'00000/00000/00000/11111/00000/00000/00000',
    '/':'00001/00001/00010/00100/01000/10000/10000',
    ' ':'00000/00000/00000/00000/00000/00000/00000',
}


class Canvas:
    def __init__(self, pixels: bytes | None = None):
        self.pixels = bytearray(pixels) if pixels else bytearray(bytes(BACKGROUND) * WIDTH * HEIGHT)

    def rect(self, x, y, width, height, color):
        x, y, width, height = map(int, (x, y, width, height))
        left, right = max(0, x), min(WIDTH, x + width)
        if right <= left:
            return
        row = bytes(color) * (right - left)
        for py in range(max(0, y), min(HEIGHT, y + height)):
            offset = (py * WIDTH + left) * 3
            self.pixels[offset:offset + len(row)] = row

    def dot(self, x, y, radius, color):
        for dy in range(-radius, radius + 1):
            half = int(math.sqrt(max(0, radius * radius - dy * dy)))
            self.rect(round(x) - half, round(y) + dy, half * 2 + 1, 1, color)

    def line(self, start, end, color, thickness=4):
        dx, dy = end[0] - start[0], end[1] - start[1]
        steps = max(abs(dx), abs(dy), 1)
        for i in range(int(steps) + 1):
            self.dot(start[0] + dx * i / steps, start[1] + dy * i / steps, thickness // 2, color)

    def text(self, x, y, value, scale=3, color=WHITE):
        for character in value.upper():
            for row, bits in enumerate(_GLYPHS[character].split('/')):
                for column, bit in enumerate(bits):
                    if bit == '1':
                        self.rect(x + column * scale, y + row * scale, scale, scale, color)
            x += scale * 6

    def symbol(self, center, shape, radius=29):
        x, y = center
        if shape == 'Square':
            points = [(x-radius,y-radius),(x+radius,y-radius),(x+radius,y+radius),(x-radius,y+radius)]
        elif shape == 'Triangle':
            points = [(x,y-radius),(x+radius,y+radius),(x-radius,y+radius)]
        else:
            points = [(round(x+radius*math.cos(i*math.pi/24)),round(y+radius*math.sin(i*math.pi/24))) for i in range(48)]
        for start, end in zip(points, points[1:] + points[:1]):
            self.line(start, end, WHITE, 6)


def chapter_base(index: int) -> bytes:
    chapter = CHAPTERS[index]
    accent = tuple(bytes.fromhex(chapter['color']))
    c = Canvas()
    c.rect(0, 0, WIDTH, 12, accent)
    c.text(48, 40, 'ANNOTATION EVIDENCE', 3, MUTED)
    c.text(48, 108, f'0{index + 1}', 9)
    c.text(48, 194, chapter['title'], 6)
    c.symbol((78, 290), chapter['shape'])
    c.text(132, 278, chapter['shape'], 4)
    c.text(48, 356, f"RANGE 00:{index * 4:02d} - 00:{(index + 1) * 4:02d}", 3, MUTED)
    # Fixed isometric cube: the marker traces the feature linked to the annotation.
    c.rect(608, 98, 304, 288, accent)
    front = [(646,216),(800,216),(800,352),(646,352)]
    back = [(730,154),(884,154),(884,290),(730,290)]
    for polygon in (front, back):
        for start, end in zip(polygon, polygon[1:] + polygon[:1]):
            c.line(start, end, BACKGROUND, 4)
    for start, end in zip(front, back):
        c.line(start, end, BACKGROUND, 4)
    paths = [front + [front[0]], [front[0], front[1]], [front[1],back[1],back[2],front[2],front[1]]]
    for start, end in zip(paths[index], paths[index][1:]):
        c.line(start, end, BACKGROUND, 13)
        c.line(start, end, WHITE, 7)
    c.text(48, 434, 'VIDEO TIME', 3, MUTED)
    c.text(738, 434, '/ 00:12.0', 3, MUTED)
    c.rect(48, 492, 864, 12, (63, 80, 100))
    for x in (336, 624):
        c.rect(x, 490, 3, 16, MUTED)
    return bytes(c.pixels)


def render_frame(frame: int, bases: tuple[bytes, ...] | None = None) -> bytes:
    if not 0 <= frame < FPS * 12:
        raise ValueError('Frame must fall inside the 12-second demonstration.')
    index = frame // (FPS * 4)
    c = Canvas(bases[index] if bases else chapter_base(index))
    progress = (frame % (FPS * 4)) / (FPS * 4 - 1)
    paths = [((646,216),(800,216),(800,352),(646,352),(646,216)),
             ((646,216),(800,216)),
             ((800,216),(884,154),(884,290),(800,352),(800,216))]
    path = paths[index]
    position = progress * (len(path) - 1)
    step = min(int(position), len(path) - 2)
    a, b = path[step], path[step + 1]
    fraction = position - step
    point = (a[0] + (b[0]-a[0])*fraction, a[1] + (b[1]-a[1])*fraction)
    c.dot(*point, 12, BACKGROUND)
    c.dot(*point, 7, WHITE)
    # Absolute time remains truthful in derived clips and discontinuous sequences.
    tenths = frame * 10 // FPS
    c.text(486, 428, f'00:{tenths // 10:02d}.{tenths % 10}', 4)
    c.rect(48, 492, round(864 * frame / (FPS * 12 - 1)), 12, WHITE)
    return bytes(c.pixels)
