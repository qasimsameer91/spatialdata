# spatialdata — marketing site

Static, five pages, no build step. Plain HTML/CSS/JS to match the product's own
dashboard, so there is nothing to compile before deploying.

```
site/
  index.html      hero, problem, real stills, how it works, CTA
  features.html   the five data layers + engineering detail
  gallery.html    playable films and stills, all real output
  pricing.html    three tiers, FAQ, checkout hooks
  docs.html       install, job file, beats, hosted queue, licence
  assets/
    css/site.css  design system + motion
    js/site.js    reveals, parallax, counters, checkout
    img/  video/  frames and clips taken from actual renders
```

## Run it locally

```bash
cd site
python -m http.server 8090
# http://127.0.0.1:8090
```

## Wire up payments

Nothing is connected to a payment processor. Each buy button carries a
`data-plan` attribute; fill in the matching URL at the top of
`assets/js/site.js`:

```js
var CHECKOUT_URLS = {
  solo:       "https://buy.stripe.com/...",
  studio:     "https://buy.stripe.com/...",
  enterprise: "mailto:you@example.com?subject=Broadcast%20licence"
};
```

Any hosted checkout works — Stripe Payment Links, Paddle, Lemon Squeezy,
Gumroad. Until a URL is set, clicking a buy button shows an inline note saying
so rather than failing silently.

**The prices in `pricing.html` are placeholders.** $149 / $449 / "talk to us"
are a starting shape, not a recommendation — set them to whatever you intend to
charge.

## Deploy

Everything is static, so any host works: Netlify, Cloudflare Pages, GitHub
Pages, or the same Railway project serving a second static service. Point the
host at `site/` as the publish directory. There is no build command.

## Media

Every image and clip in `assets/` came out of the renderer — the Karakoram,
the Indus, and the 2022 Sindh floods. Regenerate them after a new render with
`ffmpeg`; the hero loop is 9 seconds at CRF 30, the gallery films are CRF 27 at
1280px wide.

Keep the attribution line in the footer: the underlying map, imagery and
population data are free but require credit.
