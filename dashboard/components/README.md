# Dashboard Components

This directory contains the reusable UI pieces for the dashboard. It is split between low-level shadcn/ui primitives and feature-specific components that encode the product behavior of the operator experience.

The key idea here is that not every visual element should be a product component. Some pieces are true primitives that can be reused across pages, while others are domain-specific views that only make sense in the cluster and incident workflows.

## Subdirectories

- [ui/](ui/) contains the primitive building blocks such as buttons, cards, tables, inputs, badges, and scroll areas.
- [dashboard/](dashboard/) contains the feature components used by the actual cluster and incident screens.

## Design Boundary

Use [ui/](ui/) when you need a styled primitive and use [dashboard/](dashboard/) when the component has product-specific behavior, data fetching, or dashboard semantics.

The feature components are client-side and usually depend on the auth context API helper from [../lib/README.md](../lib/README.md). That dependency is a good sign that the component is part of a live workflow rather than a passive display element.

## What Belongs Where

### Put It In `ui/` When

- The component is a generic visual primitive.
- It has no knowledge of clusters, incidents, or auth state.
- It can be reused in multiple unrelated screens.

### Put It In `dashboard/` When

- The component knows about incidents, cluster context, or account state.
- It fetches data or interprets API payloads.
- It owns a workflow like refreshing a transcript, rendering a summary, or showing current status.

## Why This Boundary Matters

The dashboard is easier to maintain when the primitive layer stays simple and the feature layer stays honest about domain behavior. If a component starts to know too much about dashboard state, it is usually a feature component. If it is only concerned with styling and composition, it belongs in `ui/`.

## What To Extend

- Add new visual primitives to `ui/` only when they are broadly reusable.
- Add new feature components to `dashboard/` when the component owns a dashboard workflow or data shape.
- Keep component logic close to the page or workflow it serves so the data dependencies remain obvious.
- Prefer to keep data fetching close to the feature component that needs the data, not inside the primitive layer.

## Related Docs

- [../README.md](../README.md)
- [../app/README.md](../app/README.md)
- [../lib/README.md](../lib/README.md)