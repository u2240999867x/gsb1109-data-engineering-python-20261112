.. _relational_data_generation:

==========================
Relational Data Generation
==========================

.. versionadded:: 20.0.0

:class:`~mimesis.schema.Schema` is ideal for independent documents (see
:doc:`schema`). When you need **related** collections — users and posts,
orders and line items, etc. — use :class:`~mimesis.builder.SchemaBuilder`.

:class:`~mimesis.builder.SchemaBuilder` lets you:

- Define multiple named schemas that share one locale and seed
- Link records with **foreign keys** (``sb.ref(users).id``)
- Embed a **whole related record** (``sb.ref(users)``)
- **Nest** child documents inline (``addresses(count=2)``)
- Generate everything in one ``create()`` call with **automatic dependency
  resolution** (generation order does not depend on kwarg order)

Quick Start
-----------

Create users and posts where each post references a user:

.. code-block:: python

    from mimesis import SchemaBuilder
    from mimesis.locales import Locale

    sb = SchemaBuilder(Locale.EN, seed=0xFF)

    users = sb.schema("users", {
        "id": sb.f("increment", accumulator="user"),
        "username": sb.f("username"),
        "email": sb.f("email"),
    })

    posts = sb.schema("posts", {
        "id": sb.f("increment", accumulator="post"),
        "title": sb.f("sentence"),
        "user_id": sb.ref(users).id,  # Foreign key to users
    })

    # Order of kwargs does not matter — dependencies are resolved automatically
    data = sb.create(posts=20, users=5)

    print(len(data["users"]))  # 5
    print(len(data["posts"]))  # 20
    print(data["posts"][0]["user_id"] in {u["id"] for u in data["users"]})
    # True

Defining Fields with ``f()``
-----------------------------

:meth:`~mimesis.builder.SchemaBuilder.f` creates a lazy field that is evaluated
when data is generated. The first argument is a provider method name (the same
names you pass to :class:`~mimesis.schema.Field`). Extra keyword arguments are
forwarded to that method:

.. code-block:: python

    sb.f("username")
    sb.f("email", domains=["example.com"])
    sb.f("increment", accumulator="user")
    sb.f("timestamp", fmt=TimestampFormat.POSIX)

You can also pick from fixed or weighted lists without a provider method:

.. code-block:: python

    sb.choice(["active", "archived", "draft"])
    sb.weighted_choice(["common", "rare"], [0.9, 0.1])

Key Functions with ``f()``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The optional ``key`` parameter accepts the same callables as
:class:`~mimesis.schema.Field`, including everything in :mod:`mimesis.keys`
(see :ref:`key_functions`):

.. code-block:: python

    from mimesis import SchemaBuilder
    from mimesis.keys import maybe, pipe, slugify, prefix
    from mimesis.locales import Locale

    sb = SchemaBuilder(Locale.EN, seed=0xFF)

    users = sb.schema("users", {
        "id": sb.f("increment", accumulator="user"),
        "username": sb.f(
            "username",
            key=pipe(slugify, prefix("user-")),
        ),
        "email": sb.f("email", key=maybe(None, probability=0.2)),
        "display_name": sb.f("full_name", key=str.upper),
    })

Foreign Keys and Whole Records
------------------------------

``sb.schema()`` returns a :class:`~mimesis.builder.SchemaRef`. Pass that
reference to :meth:`~mimesis.builder.SchemaBuilder.ref` to link schemas:

- **Foreign key** — ``sb.ref(users).id`` picks a random value of the ``id``
  field from already generated ``users`` rows
- **Whole record** — ``sb.ref(users)`` embeds a random full user dict

.. code-block:: python

    users = sb.schema("users", {
        "id": sb.f("increment", accumulator="user"),
        "username": sb.f("username"),
    })

    posts = sb.schema("posts", {
        "id": sb.f("increment", accumulator="post"),
        "user_id": sb.ref(users).id,   # scalar FK
        "author": sb.ref(users),       # embedded user document
        "title": sb.f("sentence"),
    })

    data = sb.create(users=3, posts=10)

.. note::
   Every schema referenced with ``sb.ref()`` must be included in the same
   ``create()`` call (for example ``users=3``), so parent rows exist before
   foreign keys are resolved.

Nested Documents
----------------

Call a :class:`~mimesis.builder.SchemaRef` with ``count=N`` to embed ``N``
generated child documents inside a parent field. Nested schemas used **only**
this way do not need to be passed to ``create()``:

.. code-block:: python

    addresses = sb.schema("addresses", {
        "city": sb.f("city"),
        "street": sb.f("street_name"),
        "zip_code": sb.f("zip_code"),
    })

    customers = sb.schema("customers", {
        "id": sb.f("increment", accumulator="customer"),
        "name": sb.f("full_name"),
        "addresses": addresses(count=2),
    })

    data = sb.create(customers=10)

Dependency Resolution
---------------------

:meth:`~mimesis.builder.SchemaBuilder.create` topologically sorts schemas from
foreign-key references. You can pass counts in any order:

.. code-block:: python

    data = sb.create(api_keys=10, projects=5, users=3)
    # users → projects → api_keys

Lifecycle Helpers
-----------------

:class:`~mimesis.builder.SchemaBuilder` provides helpers for controlling state
between runs:

- :meth:`~mimesis.builder.SchemaBuilder.reseed` — change the random seed
- :meth:`~mimesis.builder.SchemaBuilder.clear` — drop generated data, keep
  schema definitions
- :meth:`~mimesis.builder.SchemaBuilder.reset` — clear schemas and generated
  data

.. code-block:: python

    data_a = sb.create(users=5)
    sb.clear()
    data_b = sb.create(users=5)  # same schemas, fresh data

    sb.reseed(0xAB)
    data_c = sb.create(users=5)

    sb.reset()  # definitions are gone; call sb.schema() again

Batch Snapshots and Replacing Schemas
--------------------------------------

Every :meth:`~mimesis.builder.SchemaBuilder.create` call is a **batch**. When a
batch starts, the builder snapshots all registered definitions *and* derives
the dependency graph from that same snapshot. The batch therefore always works
from one consistent, immutable view: mutating the dictionary you originally
passed to :meth:`~mimesis.builder.SchemaBuilder.schema` while a batch is
running (or between batches) cannot make the graph and the generated
definitions drift apart. Changes are picked up by the *next* batch, and that
next batch reads the whole new set together.

The snapshot only recreates the surrounding dicts, lists and tuples. Lazy
objects such as :meth:`~mimesis.builder.SchemaBuilder.f`,
:meth:`~mimesis.builder.SchemaBuilder.choice` and references are kept by
reference, so a lazy object shared by several columns stays the same instance
and any state held by its provider or key callback is preserved.

To change a table deliberately, use
:meth:`~mimesis.builder.SchemaBuilder.replace_schema` instead of mutating the
definition in place:

.. code-block:: python

    users = sb.schema("users", {
        "id": sb.f("increment"),
        "name": sb.f("username"),
    })

    sb.create(users=3)  # batch 1: old definition

    sb.replace_schema("users", {
        "id": sb.f("increment"),
        "email": sb.f("email"),
    })

    sb.create(users=3)  # batch 2: new definition, existing refs/FKs still work

The replacement is validated before it is applied. If it would introduce a
dependency cycle, :class:`ValueError` is raised and the builder is left
untouched, so the current batch and the next one both keep emitting rows on
the original acyclic graph.

Multi-level Example
-------------------

A small graph with users, projects, and API keys:

.. code-block:: python

    from mimesis import SchemaBuilder
    from mimesis.enums import TimestampFormat
    from mimesis.locales import Locale

    sb = SchemaBuilder(Locale.EN, seed=0xFF)

    users = sb.schema("users", {
        "id": sb.f("increment", accumulator="user"),
        "username": sb.f("username"),
        "email": sb.f("email"),
        "created_at": sb.f("timestamp", fmt=TimestampFormat.POSIX),
    })

    projects = sb.schema("projects", {
        "id": sb.f("increment", accumulator="project"),
        "name": sb.f("word"),
        "version": sb.f("version"),
        "owner_id": sb.ref(users).id,
        "status": sb.choice(["active", "archived", "draft"]),
    })

    api_keys = sb.schema("api_keys", {
        "id": sb.f("uuid"),
        "key": sb.f("token_hex"),
        "project_id": sb.ref(projects).id,
        "created_at": sb.f("timestamp", fmt=TimestampFormat.POSIX),
    })

    data = sb.create(
        api_keys=10,
        projects=5,
        users=3,
    )

This generates:

- 3 users
- 5 projects (each with a valid ``owner_id`` referencing a user)
- 10 API keys (each with a valid ``project_id`` referencing a project)

See also: :class:`~mimesis.builder.SchemaBuilder` API reference.
