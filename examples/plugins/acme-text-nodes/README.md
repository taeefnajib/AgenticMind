# acme-text-nodes

AgenticMind nodes by `acme`.

```
agenticmind-node validate .    # check the node contracts
agenticmind-node test .        # run the tests
agenticmind-node pack .        # build dist/*.whl
```

Install the wheel in AgenticMind under **Node packages**, review the permissions it requests and approve
them. The nodes then appear in the node library and run in an isolated worker process.

Rules: node types live in your own namespace (`acme.…`), versions are semver, and every permission a node
uses must be declared (`network`, `credentials`, `filesystem`, `subprocess`, `database`). Secrets are
referenced in config (`{"$secret": "NAME"}`) and read with `ctx.secret(...)`.
