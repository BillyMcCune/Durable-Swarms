# Architecture section

`sections/architecture.tex` is the report-ready architecture section. It describes
the implemented simulator and recovery protocol without claiming benchmark
results, general external-tool exactly-once execution, or unimplemented optimizations.

`main.tex` is a standalone, two-column preview wrapper. It omits a title block
because this artifact is one section of the larger report. The PDF metadata
identifies Pete Crowley, Billy McCune, and Jonathan Ouwerx as authors.

The unmodified official `usenix2019_v3.sty` is distributed from:

- Template page: https://www.usenix.org/conferences/author-resources/paper-templates
- Style file: https://www.usenix.org/sites/default/files/usenix2019_v3.sty

Build from this directory with an installed LaTeX distribution:

```sh
pdflatex -interaction=nonstopmode -halt-on-error main.tex
pdflatex -interaction=nonstopmode -halt-on-error main.tex
```

For the full report, copy the section and use `\input{sections/architecture}` at
the desired location. Its section numbering and cross-references will adapt to
the containing document. The preamble needs the official USENIX style,
`amsmath`, `tikz`, and TikZ libraries `arrows.meta,positioning`, as shown in `main.tex`.

The generated preview is delivered at `../output/pdf/architecture.pdf`.
