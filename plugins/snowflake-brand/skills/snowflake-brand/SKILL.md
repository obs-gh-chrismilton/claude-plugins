---
name: snowflake-brand
description: "CRITICAL: This skill contains exact Snowflake brand specifications (hex colors, button dimensions, font names, logo files, partner layout rules) that you CANNOT accurately reproduce from memory. Always consult this skill when the user is doing ANY visual, design, or document work involving Snowflake — including building Snowflake-branded web pages, creating co-branded presentations or slide decks, writing proposals or documents for Snowflake partnerships, generating CSS/HTML with Snowflake colors, designing emails or marketing materials mentioning Snowflake, asking about Snowflake's color palette or typography, reviewing designs for Snowflake brand compliance, or requesting Snowflake logo files. Do NOT trigger for Snowflake database operations (SQL queries, warehouse config, Snowpipe, dbt, RBAC, data engineering). The distinguishing signal is whether the task involves visual appearance, branding, or document formatting versus data platform operations."
---

# Snowflake Brand Guidelines

This skill contains the official Snowflake brand specifications sourced from https://www.snowflake.com/brand-guidelines/. Use it to ensure all visual output — code, designs, presentations, documents, and partner materials — is brand-compliant.

## When to Apply These Guidelines

- Building web pages, components, or apps that represent Snowflake or a Snowflake partnership
- Creating presentations, proposals, or pitch decks involving Snowflake
- Designing emails, social media assets, or marketing collateral
- Writing CSS/HTML that should match Snowflake's visual identity
- Reviewing designs for Snowflake brand compliance
- Creating co-branded materials with Snowflake partner logos

## Core Brand Identity

### Color System

Snowflake's palette is built around three tiers. Always prefer primary colors for dominant UI elements, with secondary and tertiary colors for accents and backgrounds.

**Primary Colors** (use for main brand elements, CTAs, headers):

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| Snowflake Blue | `#29B5E8` | 41, 181, 232 | Primary brand color. CTAs, links, key UI elements |
| Mid Blue | `#11567F` | 17, 86, 127 | Secondary text, hover states, depth |
| Midnight | `#000000` | 0, 0, 0 | Body text, high-contrast elements |

**Secondary Colors** (use for accents, data visualization, supporting elements):

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| Star Blue | `#71D3DC` | 113, 221, 220 | Light accents, secondary highlights |
| Valencia Orange | `#FF9F36` | 255, 159, 54 | Warm accents, alerts, emphasis |
| Purple Moon | `#7D44CF` | 125, 68, 207 | Feature highlights, categories |
| First Light | `#D45B90` | 212, 91, 144 | Decorative accents |
| Windy City | `#8A999E` | 138, 153, 158 | Neutral accents, muted text, borders |

**Tertiary Colors** (use for dark backgrounds, gradients, depth):

| Name | Hex | RGB | Usage |
|------|-----|-----|-------|
| Iceberg | `#003545` | 0, 53, 69 | Dark backgrounds, hero sections |
| Ruby Sky | `#3C0045` | 60, 0, 69 | Dark gradient accents |
| Violet Dawn | `#1C0045` | 28, 0, 69 | Deep background gradients |
| Winter | `#24323D` | 36, 50, 61 | Dark UI surfaces, footers |

For the full color spec including CMYK and PMS (Pantone) values for print work, read `references/detailed-specs.md`.

### Typography

**Headlines & Display Text**: TEXTA typeface family
- Use for headings, hero text, navigation labels, and button text
- Available in multiple weights; Heavy weight for buttons and key CTAs

**Body & Subhead Text**: Lato (freely available via Google Fonts)
- Use for body copy, subheadings, descriptions, and form labels
- Clean and highly legible at all sizes

When TEXTA is unavailable (e.g., web projects without the licensed font), substitute with a geometric sans-serif like Montserrat or Nunito Sans as a reasonable fallback — but note this is not an official substitution. For CSS, load Lato from Google Fonts: `@import url('https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap');`

### Logo Usage

**Two official variants:**
1. **Snowflake Blue logo** — use on white or light dotted/patterned backgrounds
2. **White logo** — use on Snowflake Blue backgrounds, color-overlay images, and non-white specialty items

**Rules:**
- Maintain minimum clear space on all sides equal to the x-height of the logotype
- The Snowflake bug (icon) must always include the registration mark (R)
- When the bug is enlarged, the registration mark should be one-third the size of an arrow leg, placed at the center point of the 2 and 3 o'clock arrows' intersection
- Never distort, rotate, recolor, or add effects to the logo
- Never place the logo on busy or low-contrast backgrounds

## Applying the Brand in Code

### CSS Variables Pattern

When building Snowflake-branded interfaces, define the palette as CSS custom properties:

```css
:root {
  /* Primary */
  --sf-blue: #29B5E8;
  --sf-mid-blue: #11567F;
  --sf-midnight: #000000;

  /* Secondary */
  --sf-star-blue: #71D3DC;
  --sf-valencia-orange: #FF9F36;
  --sf-purple-moon: #7D44CF;
  --sf-first-light: #D45B90;
  --sf-windy-city: #8A999E;

  /* Tertiary */
  --sf-iceberg: #003545;
  --sf-ruby-sky: #3C0045;
  --sf-violet-dawn: #1C0045;
  --sf-winter: #24323D;

  /* Typography */
  --sf-font-heading: 'Texta', 'Montserrat', sans-serif;
  --sf-font-body: 'Lato', sans-serif;
}
```

### Button Specifications

Snowflake buttons follow specific dimension and style rules. Both flat and outlined variants exist for desktop/mobile and social media contexts.

**Desktop/Mobile Buttons:**
- Height: 34px
- Max width: 195px
- Padding: 12px top/bottom, 27px left/right
- Corner radius: 80px (pill shape)
- Font: TEXTA Heavy, ALL CAPS
- Max 13 characters
- Outlined variant adds a 1px border

**Social Media Buttons:**
- Height: 28px
- Max width: 195px
- Padding: 11px top/bottom, 22px left/right
- Corner radius: 64px
- Font: TEXTA Heavy, ALL CAPS
- Max 13 characters

```css
.sf-button {
  height: 34px;
  max-width: 195px;
  padding: 12px 27px;
  border-radius: 80px;
  font-family: var(--sf-font-heading);
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 0.02em;
  font-size: 14px;
  border: none;
  cursor: pointer;
}

.sf-button--primary {
  background-color: var(--sf-blue);
  color: #ffffff;
}

.sf-button--outlined {
  background-color: transparent;
  border: 1px solid var(--sf-blue);
  color: var(--sf-blue);
}

.sf-button--social {
  height: 28px;
  padding: 11px 22px;
  border-radius: 64px;
}
```

## Partner & Co-Branding

When placing the Snowflake logo alongside partner logos:

- All logos must be **center-aligned**
- Logos must be **spaced equally**
- Logos should appear **optically the same size** (match visual weight, not just pixel dimensions)
- Supported layouts: horizontal co-partnership, vertical co-partnership, horizontal multi-partnership, vertical multi-partnership

For partner-specific guidelines including the GTM Guidebook and Content Guide, refer to the Snowflake Partner Network resources on Seismic.

## Data Visualization Colors

When building charts, graphs, or dashboards in Snowflake's brand, use this recommended sequence for data series:

1. Snowflake Blue (`#29B5E8`) — primary series
2. Valencia Orange (`#FF9F36`) — secondary series
3. Purple Moon (`#7D44CF`) — tertiary series
4. Star Blue (`#71D3DC`) — fourth series
5. First Light (`#D45B90`) — fifth series
6. Windy City (`#8A999E`) — sixth series / neutral

This ordering maximizes contrast between adjacent series while staying on-brand.

## Brand Compliance Checklist

Before finalizing any Snowflake-branded deliverable, verify:

- [ ] Primary color is Snowflake Blue (#29B5E8), not approximated
- [ ] Logo has proper clear space and uses the correct variant for the background
- [ ] Typography uses Lato for body text (or TEXTA for headlines if available)
- [ ] Buttons follow the pill-shape spec (80px radius for desktop, 64px for social)
- [ ] Button text is ALL CAPS, max 13 characters
- [ ] Partner logos are center-aligned and optically sized
- [ ] No unapproved color modifications to the Snowflake logo
- [ ] Dark backgrounds use tertiary palette colors (Iceberg, Winter, Violet Dawn)

## Bundled Assets

This skill includes official Snowflake brand assets in the `assets/` directory. When building deliverables, reference these files directly rather than asking the user to download them separately.

### Logos (assets/logos/Snowflake Logo/)
- **Digital/SVG/**: Vector logos for web — `snowflake-logo-color-rgb.svg`, `snowflake-logo-reverse-rgb.svg`, `snowflake-bug-color-rgb.svg`, `snowflake-bug-reverse-rgb.svg`
- **Digital/PNG/**: Raster logos at @1x and @2x — same naming as SVG with resolution suffix
- **Digital/EPS/**: Vector for design tools
- **Print/EPS/**: CMYK versions for print production

### Other Assets
- `assets/partner-guidelines/` — Partner logo arrangement PDF
- `assets/Snowflake-Brand-Book.pdf` — Full brand book (32 MB)
- `assets/Snowflake_Template_2025.pptx.zip` — Official 2025 PowerPoint template
- `assets/Snowflake-Graphics-Package.zip` — Dot backgrounds and decorative elements
- `assets/colors/` — Adobe .ase swatches and Sketch palette files

When creating documents or presentations, use or reference the 2025 PowerPoint template. When building web pages, embed the SVG logos directly. For partner materials, consult the partner guidelines PDF.

## Document & Presentation Guidelines

When creating Snowflake-branded documents (proposals, reports, internal docs):

**Customer-Facing Documents:**
- Use Snowflake Blue (#29B5E8) as the primary accent color for headers, dividers, and highlights
- Body text in Midnight (#000000) using Lato font
- Include the official Snowflake logo (correct variant for background) with proper clear space
- Use the 2025 PowerPoint template as a starting point for presentations
- Partner logos must follow co-branding rules (center-aligned, optically equal size)
- All CTAs and emphasis text should use Snowflake Blue

**Internal Documents:**
- Same color palette applies, but you have more flexibility with layout
- Tertiary colors (Iceberg, Winter) work well for section backgrounds and sidebar elements
- Secondary colors can be used more liberally for visual hierarchy
- Data visualization should still follow the recommended color sequence
- The brand book PDF in `assets/` is the authoritative reference for any edge cases

## Reference

For complete specifications including CMYK/PMS print values, full button dimension tables, and downloadable asset URLs, read `references/detailed-specs.md`.
