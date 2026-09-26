# Music Streaming Web Application — Design System & UI Architecture

This specification document outlines the design system, structural layout, and user experience patterns for the music streaming platform, borrowing key UX interactions from **Spotify** (layout grid, persistent player, library organization) and **YouTube Music** (mood chips, rich video/audio toggles, visual carousel cards).

---

## 1. Design System: "Violet Dusk" Theme

### Color Palette (Extracted from Design Image)

| Token Name | Hex Code | Purpose & Usage |
| :--- | :--- | :--- |
| **Primary Base** | `#502D55` | Main background gradient base, deep sidebar background, header accents. |
| **Secondary Accent** | `#935073` | Active navigation states, secondary buttons, hover glow effects, audio wave visualizer. |
| **Highlight Warmth** | `#F6DBC0` | Primary call-to-action (CTA) buttons, play buttons, active sliders, key badges. |
| **Light Surface** | `#F8F4E9` | Primary text, key icons, highlighted titles, prominent UI elements. |
| **Dark Overlay** | `rgba(20, 10, 22, 0.85)` | Glassmorphism card backgrounds, modal backdrops, floating player panel. |
| **Subtle Border** | `rgba(246, 219, 192, 0.15)` | Divider lines, card borders, input borders. |

---

### Typography (Google Fonts)

To mirror the clean, modern look of **Poppins** and **Montserrat**, we use a dual-font configuration:

1. **Primary Body & Display Font**: **`Plus Jakarta Sans`** or **`Poppins`**
   * *Google Fonts URL*: `https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&family=Montserrat:wght@600;700;800&display=swap`
   * **Montserrat**: Used for bold headers, hero sections, artist names, and main titles.
   * **Poppins**: Used for body text, track listings, timestamps, metadata, and form fields.

#### Type Scale Rules

```css
:root {
  --font-heading: 'Montserrat', sans-serif;
  --font-body: 'Poppins', sans-serif;

  --fs-hero: 2.75rem;     /* 44px - Page Headers / Playlist Titles */
  --fs-h1: 1.75rem;       /* 28px - Section Headers */
  --fs-h2: 1.25rem;       /* 20px - Card Titles */
  --fs-body: 0.938rem;    /* 15px - Track titles, primary body */
  --fs-small: 0.813rem;   /* 13px - Artist names, duration, secondary text */
  --fs-tiny: 0.75rem;     /* 12px - Badges, sub-labels */
}
```

---

## 2. Global Layout Architecture

The general structure follows Spotify's 3-pane layout merged with YouTube Music's top mood filtering and sticky media player.

```
+-----------------------------------------------------------------------------+
|                               Top Bar / Search                              |
+------------------+---------------------------------------+------------------+
|                  |                                       |                  |
|   Left Sidebar   |            Main Content Area          |  Right Sidebar   |
|                  |   (Home / Library / Playlist View)    | (Now Playing /   |
|  - Navigation    |                                       |  Queue / Lyrics) |
|  - Quick Access  |  - Filter Chips (YT Music Style)      |                  |
|  - User Playlists|  - Dynamic Grids & Horizontal Scrolls |                  |
|                  |                                       |                  |
+------------------+---------------------------------------+------------------+
|                        Persistent Audio Player Bar                          |
+-----------------------------------------------------------------------------+
```

---

## 3. Page Structure & Component Design

### A. Home Page

*Inspired by YouTube Music's Mood Chips and Spotify's Quick-Play Grid.*

1. **Top Navigation Bar**:
   * App Logo (`Violet Dusk` gradient badge).
   * Search Bar with quick-filtering tags (`Songs`, `Albums`, `Artists`, `Playlists`).
   * User Profile Avatar with dropdown (Settings, Account, Dark Mode, Logout).
2. **Mood & Activity Filter Bar (YouTube Music Style)**:
   * Horizontal scrollable chips: `Relax`, `Energize`, `Focus`, `Workout`, `Commute`, `Party`.
   * Selecting a chip filters the main feed dynamically.
3. **Hero / "Jump Back In" Section (Spotify Style)**:
   * 6-card quick-access grid (2x3 on desktop) featuring recently played playlists/albums with quick play on hover.
4. **Horizontal Content Rows**:
   * *Recommended for You*: Cards with album art, title, and artist name.
   * *Similar to [Recently Played Artist]*: Contextual recommendations.
   * *Top Charts / Trending Now*: Numbered track list or large card carousel.
5. **Persistent Footer Player**:
   * **Left**: Album art, track title, artist name, favorite button (`♥`).
   * **Center**: Playback controls (Shuffle, Previous, Play/Pause, Next, Repeat) and progress bar.
   * **Right**: Audio/Video mode switch, Queue toggle, Lyrics toggle, Volume slider.

---

### B. Login / Sign-In Page

*Minimalist, Glassmorphic aesthetic utilizing the primary color palette.*

1. **Background**:
   * Dynamic linear gradient shifting from `#502D55` to deep purple/black with a subtle background mesh.
2. **Centered Auth Card**:
   * `background: rgba(80, 45, 85, 0.4)` with CSS `backdrop-filter: blur(20px)`.
   * Border: `1px solid rgba(246, 219, 192, 0.2)`.
3. **Form Components**:
   * **App Brand**: Logo and title styled in `Montserrat`.
   * **Social Login Buttons**: Continue with Google / Apple / Spotify with light border highlight (`#F6DBC0`).
   * **Divider**: `"OR"` divider line using `#935073`.
   * **Input Fields**: Floating label text, input fields with rounded corners (`border-radius: 12px`), dark translucent background, focus outline in `#F6DBC0`.
   * **Primary Button**: Solid `#F6DBC0` background with dark text `#502D55` for high contrast readability.
4. **Footer Link**: "Don't have an account? Sign Up".

---

### C. Library Page

*Inspired by Spotify's sidebar/main library view combined with YouTube Music's tabbed layout.*

1. **Header Tabs**:
   * Filter tabs: `Playlists`, `Podcasts`, `Albums`, `Artists`, `Downloaded`.
2. **Library Controls**:
   * Search within library field.
   * Sort dropdown (`Recently Added`, `Alphabetical`, `Creator`).
   * Layout toggle: Grid View vs. List View.
3. **Grid Layout (Albums / Playlists)**:
   * Square cards with rounded corners (`12px`).
   * Pinned items badge (e.g., "Liked Songs" card pinned at top left with custom gradient).
4. **List Layout (Songs / Tracks)**:
   * Columns: `#` | `Title` | `Album` | `Date Added` | `Duration` | `...` (More Options).
   * Hover effect: Highlight row in `#935073` at low opacity (`0.2`), show play icon over track index.

---

### D. Playlist Details Page

*Inspired by Spotify's hero header layout and YouTube Music's quick action bar.*

1. **Playlist Hero Header**:
   * Large Playlist Cover Art (230x230px) with drop shadow.
   * Metadata: Public/Private tag, Title (`Montserrat`, 44px), Description, Creator avatar, total track count, total playback time.
   * Background blur: Dynamic tint extracted from cover art blending into `#502D55`.
2. **Action Bar**:
   * Large circular Play/Pause button (`#F6DBC0` fill, `#502D55` icon).
   * Shuffle Button.
   * Save / Favorite (`♥`).
   * Download for Offline toggle.
   * More options (`...`).
3. **Tracklist Table**:
   * Sticky header row during scrolling (`#`, Title, Album, Date Added, `🕒`).
   * Interactive rows: Drag-and-drop handles for custom sorting.
   * Active playing track highlight: Accent text in `#F6DBC0` with an animated sound equalizer indicator.