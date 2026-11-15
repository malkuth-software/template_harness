# {{title}} integration queue

Coders append one entry per finished branch; the integrator takes entries in
batches; only the coordinator commits this file.

Format:

```text
## <branch> @ <head>
- workspace: <path>   base: <commit>
- intent: <one line>
- touched: <files and areas>
- tests: <which tests should cover it>
- risks: <anything that could not be checked without a build>
```
