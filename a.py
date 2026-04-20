import matplotlib.pyplot as plt
import random

def draw_sierpinski_carpet(iterations=50000):

    transformations = [
        lambda x, y: (x/3, y/3),
        lambda x, y: (x/3 + 1/3, y/3),
        lambda x, y: (x/3 + 2/3, y/3),
        lambda x, y: (x/3, y/3 + 1/3),
        lambda x, y: (x/3 + 2/3, y/3 + 1/3),
        lambda x, y: (x/3, y/3 + 2/3),
        lambda x, y: (x/3 + 1/3, y/3 + 2/3),
        lambda x, y: (x/3 + 2/3, y/3 + 2/3)
    ]

    x, y = random.random(), random.random()

    xs, ys = [], []

    for i in range(iterations):
        f = random.choice(transformations)
        x, y = f(x, y)

        if i > 100:
            xs.append(x)
            ys.append(y)

    plt.figure(figsize=(8, 8))
    plt.scatter(xs, ys, s=0.1, c='black', marker='.')
    plt.title(f"Sierpinski Carpet (Chaos Game - {iterations} points)")
    plt.axis('equal')
    plt.axis('off')
    plt.show()

draw_sierpinski_carpet()
