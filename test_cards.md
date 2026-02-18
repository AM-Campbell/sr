---
sr_adapter: basic_qa
tags: [test, python]
---

Q: What is a Python decorator?
A: A function that wraps another function, modifying its behavior. Defined with `@decorator` syntax above the function definition.

Q: What is the difference between a list and a tuple?
A: Lists are mutable (can be changed after creation), tuples are immutable. Lists use `[]`, tuples use `()`.

Q: What does `*args` do in a function signature?
A: It collects extra positional arguments into a tuple. For example: `def f(*args)` lets you call `f(1, 2, 3)` and `args` will be `(1, 2, 3)`.

Q: What is a list comprehension?
A: A concise syntax for creating lists: `[expression for item in iterable if condition]`. Example: `[x**2 for x in range(10) if x % 2 == 0]`

Q: What is the GIL (Global Interpreter Lock)?
A: The Global Interpreter Lock — a mutex in CPython that allows only one thread to execute Python bytecode at a time. It simplifies memory management but limits true parallelism for CPU-bound threads.
