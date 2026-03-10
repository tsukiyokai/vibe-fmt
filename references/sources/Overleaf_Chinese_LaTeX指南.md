# Chinese Typesetting in LaTeX on Overleaf

Source: https://www.overleaf.com/learn/latex/Chinese

## Overview

The recommended approach is to use the XeLaTeX or LuaLaTeX compilers because they directly support UTF-8 encoded text and work with TrueType and OpenType fonts.

## Three Main Approaches

### 1. CTeX Classes (Recommended)

The guide recommends using CTeX document classes like `ctexart`, `ctexrep`, `ctexbook`, and `ctexbeamer` for Simplified Chinese typesetting. These classes provide Chinese localization similar to Babel, automatically translating elements like "Abstract" to摘要and "Table of Contents" to目录.

Basic usage:

```latex
\documentclass{ctexart}
\begin{document}
\tableofcontents
\begin{abstract}
这是简介及摘要。
\end{abstract}
```

Font customization commands include:

- `\setCJKmainfont{}` — for main text
- `\setCJKsansfont{}` — for sans serif elements
- `\setCJKmonofont{}` — for monospace text

Key benefit: Supports both Simplified and Traditional Chinese when using appropriate fonts like Fandol.

### 2. xeCJK Package with XeLaTeX

For simpler use cases, the xeCJK package offers a lightweight alternative:

```latex
\documentclass{article}
\usepackage{xeCJK}
\begin{document}
\section{前言}
这是一些文字。
```

This approach requires XeLaTeX compilation but avoids the overhead of full CTeX localization.

### 3. CJKutf8 Package with pdfLaTeX

For documents mixing Chinese with primarily Latin text, CJKutf8 provides compatibility with pdfLaTeX:

```latex
\usepackage{CJKutf8}
\begin{CJK*}{UTF8}{gbsn}
\section{前言}
\end{CJK*}
```

Font options:

- `gbsn` or `gkai` — Simplified Chinese
- `bsmi` or `bkai` — Traditional Chinese

## Key Recommendations

- Choose XeLaTeX or LuaLaTeX for best Unicode support
- Use CTeX classes for comprehensive Chinese document support
- Use xeCJK for lighter-weight Chinese text handling
- Use CJKutf8 primarily for bilingual documents with pdfLaTeX
- Ensure appropriate fonts are installed or uploaded to your project
