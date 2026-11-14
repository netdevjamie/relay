# slugify (sample app)

A deliberately small project for trying Relay. It turns a title into a URL slug:

```console
$ python3 slugify.py "Crème Brûlée, Two Ways!"
creme-brulee-two-ways
```

Run the tests with `python3 -m unittest -q`. They use only Python's standard library, so nothing needs installing.

`.relay/profile.yaml` tells Relay how to test it. See the quickstart in the Relay README for a task to run against it.
