//! The rows of an XLSX sheet part, read into exactly the cells OpenDocKit's Python reader
//! (`opendockit.extractors._openpyxl_reading._quick_cell`) gives. Anything this reader does not take on — an
//! unusual cell or row, a comment inside the sheet data, XML it is not sure a conforming parser would accept
//! as it does — raises `HandedBack`, and the Python reader reads the part again from its start.

use std::io::{BufReader, Read};

use pyo3::create_exception;
use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyList, PyString, PyTuple};
use quick_xml::events::{BytesStart, Event};
use quick_xml::Reader;

create_exception!(
    _core,
    HandedBack,
    PyException,
    "The part is left to the Python reader."
);

/// Attributes of one tag, and namespace prefixes declared at once, this reader takes on.
const MAX_ATTRIBUTES: usize = 64;

const MAIN: &[u8] = b"http://schemas.openxmlformats.org/spreadsheetml/2006/main";

fn handed_back<T>() -> PyResult<T> {
    Err(HandedBack::new_err(()))
}

/// A Python file object read through `read(n)`.
struct PySource {
    source: Py<PyAny>,
}

impl Read for PySource {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        Python::attach(|py| {
            let chunk = self
                .source
                .call_method1(py, "read", (buf.len(),))
                .map_err(|_| std::io::Error::other("read failed"))?;
            let bytes: &[u8] = chunk
                .extract(py)
                .map_err(|_| std::io::Error::other("read gave no bytes"))?;
            if bytes.len() > buf.len() {
                return Err(std::io::Error::other("read gave too much"));
            }
            buf[..bytes.len()].copy_from_slice(bytes);
            Ok(bytes.len())
        })
    }
}

/// Whether `name` is an XML name a conforming parser accepts (ASCII is checked; other characters are let through).
fn plain_name(name: &[u8]) -> bool {
    match name.first() {
        None => false,
        Some(first) if first.is_ascii_digit() || *first == b'-' || *first == b'.' => false,
        _ => name.iter().all(|b| {
            b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b':') || *b >= 0x80
        }),
    }
}

/// Whether `c` is a character XML 1.0 allows.
fn xml_char(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\u{20}'..='\u{D7FF}' | '\u{E000}'..='\u{FFFD}' | '\u{10000}'..='\u{10FFFF}')
}

/// The text of raw character data, with line ends normalized as an XML parser does; None when it is not UTF-8,
/// holds a character XML does not allow, or holds markup a conforming parser would refuse (`<`, `]]>`).
fn character_data(raw: &[u8], cdata: bool) -> Option<String> {
    let text = std::str::from_utf8(raw).ok()?;
    if !text.chars().all(xml_char)
        || (!cdata && (text.contains('<') || text.contains("]]>") || text.contains('&')))
    {
        return None;
    }
    Some(if text.contains('\r') {
        text.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        text.to_owned()
    })
}

/// The character an entity or character reference (`amp`, `#38`, `#x26`) stands for, if it is defined.
fn reference(name: &[u8]) -> Option<char> {
    let name = std::str::from_utf8(name).ok()?;
    let c = match name {
        "amp" => '&',
        "lt" => '<',
        "gt" => '>',
        "quot" => '"',
        "apos" => '\'',
        _ => {
            // Digits only: Rust's parsers also take a leading `+`, which XML does not
            let code = if let Some(hex) = name.strip_prefix("#x") {
                if !hex.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return None;
                }
                u32::from_str_radix(hex, 16).ok()?
            } else {
                let decimal = name.strip_prefix('#')?;
                if !decimal.bytes().all(|b| b.is_ascii_digit()) {
                    return None;
                }
                decimal.parse().ok()?
            };
            char::from_u32(code)?
        }
    };
    xml_char(c).then_some(c)
}

/// An attribute value with its references resolved and white space normalized as an XML parser does; None when a
/// conforming parser would refuse it.
fn attribute_value(raw: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(raw).ok()?;
    if text.contains('<') || !text.chars().all(xml_char) {
        return None;
    }
    let mut value = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(at) = rest.find('&') {
        value.extend(rest[..at].chars().map(|c| {
            if matches!(c, '\t' | '\n' | '\r') {
                ' '
            } else {
                c
            }
        }));
        let end = rest[at..].find(';')? + at;
        value.push(reference(&rest.as_bytes()[at + 1..end])?);
        rest = &rest[end + 1..];
    }
    value.extend(rest.chars().map(|c| {
        if matches!(c, '\t' | '\n' | '\r') {
            ' '
        } else {
            c
        }
    }));
    Some(value)
}

/// The attributes of a start tag as (name, value) pairs, checked as a conforming parser checks them: no name twice,
/// every prefix declared, no default namespace other than the sheet's (`redeclared`).
struct Attributes {
    pairs: Vec<(Vec<u8>, String)>,
}

impl Attributes {
    fn get(&self, name: &[u8]) -> Option<&str> {
        self.pairs
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.as_str())
    }
}

/// Prefixes declared on the open elements, by depth.
struct Scope {
    declared: Vec<(usize, Vec<u8>)>,
}

impl Scope {
    fn knows(&self, prefix: &[u8]) -> bool {
        prefix == b"xml" || self.declared.iter().any(|(_, known)| known == prefix)
    }

    fn close(&mut self, depth: usize) {
        self.declared.retain(|(at, _)| *at < depth);
    }
}

fn prefix_of(name: &[u8]) -> Option<&[u8]> {
    name.iter().position(|b| *b == b':').map(|at| &name[..at])
}

/// Read the attributes of `start` at `depth`, declaring its prefixes. None when a conforming parser would refuse the
/// tag or when it does something this reader does not take on (a default namespace other than the sheet's, a prefix
/// bound to the sheet's namespace — elements under it are sheet elements too).
fn attributes(
    start: &BytesStart,
    depth: usize,
    scope: &mut Scope,
    root: bool,
) -> Option<Attributes> {
    let mut pairs: Vec<(Vec<u8>, String)> = Vec::new();
    for attribute in start.attributes().with_checks(true) {
        let attribute = attribute.ok()?;
        if pairs.len() == MAX_ATTRIBUTES {
            return None; // checked pair by pair, many attributes would cost their square: left to the Python reader
        }
        let key = attribute.key.as_ref().as_bytes();
        if !plain_name(key) || pairs.iter().any(|(known, _)| known == key) {
            return None;
        }
        let value = attribute_value(attribute.value.as_bytes())?;
        if key == b"xmlns" {
            if !(root && value.as_bytes() == MAIN) {
                return None;
            }
        } else if let Some(prefix) = key.strip_prefix(b"xmlns:") {
            if value.as_bytes() == MAIN
                || value.is_empty()
                || prefix == b"xml"
                || prefix == b"xmlns"
            {
                return None;
            }
            if scope.declared.len() == MAX_ATTRIBUTES {
                return None;
            }
            scope.declared.push((depth, prefix.to_vec()));
        }
        pairs.push((key.to_vec(), value));
    }
    if root && !pairs.iter().any(|(key, _)| key == b"xmlns") {
        return None; // not the sheet's namespace: openpyxl's parser finds no rows in it
    }
    for (key, _) in &pairs {
        if let Some(prefix) = prefix_of(key) {
            if prefix != b"xmlns" && !scope.knows(prefix) {
                return None;
            }
        }
    }
    if let Some(prefix) = prefix_of(start.name().as_ref().as_bytes()) {
        if !scope.knows(prefix) {
            return None;
        }
    }
    Some(Attributes { pairs })
}

/// The elements openpyxl's parser reads wherever they are: rows and merge lists (and the sheet data holding rows).
fn sheet_element(name: &[u8]) -> bool {
    matches!(name, b"row" | b"mergeCells" | b"mergeCell" | b"sheetData")
}

/// What the Python side needs to make a cell's value.
struct Context {
    shared_strings: Py<PyList>,
    style_count: Option<usize>,
    date_formats: Py<PyAny>,
    timedelta_formats: Py<PyAny>,
    from_excel: Py<PyAny>,
    epoch: Py<PyAny>,
    rich_text: bool,
}

/// The column of letters `A`–`ZZZ`, as openpyxl's `column_index_from_string` gives it.
fn column_of(letters: &[u8]) -> Option<usize> {
    if letters.is_empty() || letters.len() > 3 || !letters.iter().all(u8::is_ascii_uppercase) {
        return None;
    }
    Some(letters.iter().fold(0, |column, letter| {
        column * 26 + (letter - b'A' + 1) as usize
    }))
}

fn ascii_digits(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit())
}

/// Whether `text` is a number written as Excel writes one, which Python's `int` / `float` and Rust's parsers read the same.
fn plain_number(text: &str) -> bool {
    let body = text.strip_prefix(['-', '+']).unwrap_or(text);
    let (mantissa, exponent) = match body.find(['e', 'E']) {
        Some(at) => (&body[..at], Some(&body[at + 1..])),
        None => (body, None),
    };
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    let digits = |part: &str| part.bytes().all(|b| b.is_ascii_digit());
    (!whole.is_empty() || !fraction.is_empty())
        && digits(whole)
        && digits(fraction)
        && exponent.is_none_or(|exponent| {
            ascii_digits(exponent.strip_prefix(['-', '+']).unwrap_or(exponent))
        })
}

#[pyclass(unsendable)]
struct SheetRows {
    reader: Reader<BufReader<PySource>>,
    context: Context,
    scope: Scope,
    depth: usize,
    in_sheet_data: bool,
    root_seen: bool,
    root_closed: bool,
    row_number: i64,
    buf: Vec<u8>,
    inner: Vec<u8>,
    done: bool,
}

/// What a `<c>` holds, gathered while reading it.
#[derive(Default)]
struct CellContent {
    value: Option<String>, // the text of the first `<v>`
    seen_value: bool,
    inline: Option<String>, // the text of an `<is>` holding one plain `<t>`
    inline_odd: bool,       // an `<is>` of any other shape
    seen_inline: bool,
}

impl SheetRows {
    /// The next event, checked as a conforming parser would check it; `HandedBack` for anything this reader does not take on.
    fn event(&mut self, inner: bool) -> PyResult<Event<'static>> {
        let event = self.sheet_level_event(inner)?;
        if let Event::Start(start) | Event::Empty(start) = &event {
            if sheet_element(start.name().as_ref().as_bytes()) {
                return handed_back(); // openpyxl's parser reads a row or a merge list wherever it is
            }
        }
        Ok(event)
    }

    /// `event`, letting through the sheet elements a caller reads where they stand (`__next__`, `merges`).
    fn sheet_level_event(&mut self, inner: bool) -> PyResult<Event<'static>> {
        let buf = if inner {
            &mut self.inner
        } else {
            &mut self.buf
        };
        buf.clear();
        let event = match self.reader.read_event_into(buf) {
            Ok(event) => event.into_owned(),
            Err(_) => return handed_back(),
        };
        match &event {
            Event::Text(text) => {
                let text = character_data(text.as_ref().as_bytes(), false);
                match text {
                    None => return handed_back(),
                    Some(text)
                        if (self.depth == 0)
                            && !text.chars().all(|c| matches!(c, ' ' | '\t' | '\n')) =>
                    {
                        return handed_back()
                    }
                    _ => {}
                }
            }
            Event::CData(data) => {
                if self.depth == 0 || character_data(data.as_ref().as_bytes(), true).is_none() {
                    return handed_back();
                }
            }
            Event::GeneralRef(name) => {
                if self.depth == 0 || reference(name.as_ref().as_bytes()).is_none() {
                    return handed_back();
                }
            }
            Event::Comment(comment) => {
                let text = std::str::from_utf8(comment.as_ref().as_bytes()).ok();
                if self.in_sheet_data
                    || text.is_none_or(|text| {
                        text.contains("--") || text.ends_with('-') || !text.chars().all(xml_char)
                    })
                {
                    return handed_back();
                }
            }
            Event::PI(_) => {
                if self.in_sheet_data {
                    return handed_back();
                }
            }
            Event::Decl(_) => {
                if self.root_seen || self.depth != 0 {
                    return handed_back();
                }
            }
            Event::DocType(_) => return handed_back(),
            Event::Start(start) | Event::Empty(start) => {
                if self.root_closed || !plain_name(start.name().as_ref().as_bytes()) {
                    return handed_back();
                }
            }
            Event::End(end) => {
                if !plain_name(end.name().as_ref().as_bytes()) {
                    return handed_back();
                }
            }
            Event::Eof => {
                if self.depth != 0 || !self.root_seen {
                    return handed_back();
                }
            }
        }
        Ok(event)
    }

    /// Read on to the end of the element just opened at `self.depth` (already counted), checking what it holds.
    fn skip_element(&mut self) -> PyResult<()> {
        let floor = self.depth;
        while self.depth >= floor {
            match self.event(true)? {
                Event::Start(start) => {
                    self.depth += 1;
                    if attributes(&start, self.depth, &mut self.scope, false).is_none() {
                        return handed_back();
                    }
                }
                Event::Empty(start) => {
                    if attributes(&start, self.depth + 1, &mut self.scope, false).is_none() {
                        return handed_back();
                    }
                    self.scope.close(self.depth + 1);
                }
                Event::End(_) => {
                    self.scope.close(self.depth);
                    self.depth -= 1;
                }
                Event::Eof => return handed_back(),
                _ => {}
            }
        }
        Ok(())
    }

    /// The text of the element just opened (already counted), which must hold character data only.
    fn text_of_element(&mut self) -> PyResult<String> {
        let mut text = String::new();
        loop {
            match self.event(true)? {
                Event::Text(raw) => text
                    .push_str(&character_data(raw.as_ref().as_bytes(), false).unwrap_or_default()),
                Event::CData(raw) => text
                    .push_str(&character_data(raw.as_ref().as_bytes(), true).unwrap_or_default()),
                Event::GeneralRef(name) => {
                    text.push(reference(name.as_ref().as_bytes()).unwrap_or_default())
                }
                Event::End(_) => {
                    self.scope.close(self.depth);
                    self.depth -= 1;
                    return Ok(text);
                }
                _ => return handed_back(),
            }
        }
    }

    /// The children of a `<c>` just opened (already counted).
    fn cell_content(&mut self) -> PyResult<CellContent> {
        let mut content = CellContent::default();
        loop {
            match self.event(true)? {
                Event::Start(start) => {
                    self.depth += 1;
                    let attrs = match attributes(&start, self.depth, &mut self.scope, false) {
                        Some(attrs) => attrs,
                        None => return handed_back(),
                    };
                    match start.name().as_ref().as_bytes() {
                        b"v" if !content.seen_value => {
                            content.seen_value = true;
                            let text = self.text_of_element()?;
                            content.value = (!text.is_empty()).then_some(text);
                        }
                        b"is" if !content.seen_inline => {
                            content.seen_inline = true;
                            if !attrs.pairs.is_empty() {
                                content.inline_odd = true;
                                self.skip_element()?;
                            } else {
                                self.inline_string(&mut content)?;
                            }
                        }
                        _ => self.skip_element()?,
                    }
                }
                Event::Empty(start) => {
                    if attributes(&start, self.depth + 1, &mut self.scope, false).is_none() {
                        return handed_back();
                    }
                    self.scope.close(self.depth + 1);
                    match start.name().as_ref().as_bytes() {
                        b"v" if !content.seen_value => content.seen_value = true,
                        b"is" if !content.seen_inline => {
                            content.seen_inline = true;
                            content.inline_odd = true; // `<is/>`: no plain `<t>`
                        }
                        _ => {}
                    }
                }
                Event::End(_) => {
                    self.scope.close(self.depth);
                    self.depth -= 1;
                    return Ok(content);
                }
                Event::Text(_) | Event::CData(_) | Event::GeneralRef(_) => {}
                _ => return handed_back(),
            }
        }
    }

    /// The children of an `<is>` just opened (already counted): one `<t>` holding text only, or something else.
    fn inline_string(&mut self, content: &mut CellContent) -> PyResult<()> {
        let mut children = 0;
        loop {
            match self.event(true)? {
                Event::Start(start) => {
                    self.depth += 1;
                    if attributes(&start, self.depth, &mut self.scope, false).is_none() {
                        return handed_back();
                    }
                    children += 1;
                    if start.name().as_ref().as_bytes() == b"t" && children == 1 {
                        // A `<t>` with an element inside is not plain: openpyxl's readers decide
                        let floor = self.depth;
                        let mut text = String::new();
                        let mut plain = true;
                        while self.depth >= floor {
                            match self.event(true)? {
                                Event::Text(raw) => text.push_str(
                                    &character_data(raw.as_ref().as_bytes(), false)
                                        .unwrap_or_default(),
                                ),
                                Event::CData(raw) => text.push_str(
                                    &character_data(raw.as_ref().as_bytes(), true)
                                        .unwrap_or_default(),
                                ),
                                Event::GeneralRef(name) => text
                                    .push(reference(name.as_ref().as_bytes()).unwrap_or_default()),
                                Event::Start(inner) => {
                                    plain = false;
                                    self.depth += 1;
                                    if attributes(&inner, self.depth, &mut self.scope, false)
                                        .is_none()
                                    {
                                        return handed_back();
                                    }
                                }
                                Event::Empty(inner) => {
                                    plain = false;
                                    if attributes(&inner, self.depth + 1, &mut self.scope, false)
                                        .is_none()
                                    {
                                        return handed_back();
                                    }
                                    self.scope.close(self.depth + 1);
                                }
                                Event::End(_) => {
                                    self.scope.close(self.depth);
                                    self.depth -= 1;
                                }
                                _ => return handed_back(),
                            }
                        }
                        if plain {
                            content.inline = Some(text);
                        }
                    } else {
                        self.skip_element()?;
                    }
                }
                Event::Empty(start) => {
                    if attributes(&start, self.depth + 1, &mut self.scope, false).is_none() {
                        return handed_back();
                    }
                    self.scope.close(self.depth + 1);
                    children += 1;
                    if start.name().as_ref().as_bytes() == b"t" && children == 1 {
                        content.inline = Some(String::new());
                    }
                }
                Event::End(_) => {
                    self.scope.close(self.depth);
                    self.depth -= 1;
                    if children != 1 {
                        content.inline = None;
                        content.inline_odd = true;
                    } else if content.inline.is_none() {
                        content.inline_odd = true;
                    }
                    return Ok(());
                }
                Event::Text(_) | Event::CData(_) | Event::GeneralRef(_) => {}
                _ => return handed_back(),
            }
        }
    }

    /// The cell dict of a `<c>` (opened, not yet counted when `empty`), `column` being the column counter of the row.
    fn cell<'py>(
        &mut self,
        py: Python<'py>,
        start: &BytesStart,
        empty: bool,
        column_counter: &mut usize,
    ) -> PyResult<Bound<'py, PyDict>> {
        if !empty {
            self.depth += 1;
        }
        let attrs = match attributes(
            start,
            if empty { self.depth + 1 } else { self.depth },
            &mut self.scope,
            false,
        ) {
            Some(attrs) => attrs,
            None => return handed_back(),
        };
        if empty {
            self.scope.close(self.depth + 1);
        }
        if attrs.get(b"vm").is_some() {
            return handed_back(); // an in-cell picture or data type
        }
        let data_type = attrs.get(b"t").unwrap_or("n").to_owned();
        if !matches!(
            data_type.as_str(),
            "n" | "s" | "str" | "b" | "e" | "inlineStr"
        ) {
            return handed_back();
        }
        let (row, column) = match attrs.get(b"r") {
            Some(reference) if !reference.is_empty() => {
                let bytes = reference.as_bytes();
                let split = bytes
                    .iter()
                    .position(u8::is_ascii_digit)
                    .unwrap_or(bytes.len());
                let (letters, digits) = reference.split_at(split);
                match (column_of(letters.as_bytes()), ascii_digits(digits)) {
                    (Some(column), true) => match digits.parse::<i64>() {
                        Ok(row) => (row, column),
                        Err(_) => return handed_back(),
                    },
                    _ => return handed_back(),
                }
            }
            _ => (self.row_number, *column_counter + 1),
        };
        let style_id = match attrs.get(b"s") {
            None => 0,
            Some(style) if ascii_digits(style) => match style.parse::<usize>() {
                Ok(style) if self.context.style_count.is_none_or(|count| style < count) => style,
                _ => return handed_back(),
            },
            Some(_) => return handed_back(),
        };
        let content = if empty {
            CellContent::default()
        } else {
            self.cell_content()?
        };

        let mut data_type_out = data_type.clone();
        let value: Bound<'py, PyAny> = if data_type == "inlineStr" {
            if !content.seen_inline || content.inline_odd {
                return handed_back();
            }
            let text = content.inline.unwrap_or_default();
            data_type_out = "s".to_owned();
            if self.context.rich_text {
                PyString::new(py, &text.replace("x005F_", "")).into_any()
            } else {
                PyString::new(py, &text).into_any()
            }
        } else {
            match content.value {
                None => py.None().into_bound(py),
                Some(text) => match data_type.as_str() {
                    "n" => {
                        if !plain_number(&text) {
                            return handed_back();
                        }
                        let number: Bound<'py, PyAny> = if text.contains(['.', 'E', 'e']) {
                            match text.parse::<f64>() {
                                Ok(value) if value.is_finite() => {
                                    PyFloat::new(py, value).into_any()
                                }
                                _ => return handed_back(),
                            }
                        } else {
                            match text.parse::<i64>() {
                                Ok(value) => value.into_pyobject(py)?.into_any(),
                                Err(_) => return handed_back(),
                            }
                        };
                        if self.context.date_formats.bind(py).contains(style_id)? {
                            data_type_out = "d".to_owned();
                            let kwargs = PyDict::new(py);
                            kwargs.set_item(
                                "timedelta",
                                self.context.timedelta_formats.bind(py).contains(style_id)?,
                            )?;
                            let converted = self
                                .context
                                .from_excel
                                .bind(py)
                                .call((number, self.context.epoch.bind(py)), Some(&kwargs))?;
                            if let Ok(value) = converted.cast::<PyFloat>() {
                                if !value.value().is_finite() {
                                    return handed_back();
                                }
                            }
                            converted
                        } else {
                            number
                        }
                    }
                    "s" => {
                        if !ascii_digits(&text) {
                            return handed_back();
                        }
                        let strings = self.context.shared_strings.bind(py);
                        match text.parse::<usize>() {
                            Ok(index) if index < strings.len() => strings.get_item(index)?,
                            _ => return handed_back(),
                        }
                    }
                    "b" => match text.as_str() {
                        "0" => PyBool::new(py, false).to_owned().into_any(),
                        "1" => PyBool::new(py, true).to_owned().into_any(),
                        _ => return handed_back(),
                    },
                    "str" => {
                        data_type_out = "s".to_owned();
                        PyString::new(py, &text).into_any()
                    }
                    _ => PyString::new(py, &text).into_any(), // "e"
                },
            }
        };
        *column_counter = column;
        let cell = PyDict::new(py);
        cell.set_item("row", row)?;
        cell.set_item("column", column)?;
        cell.set_item("value", value)?;
        cell.set_item("data_type", data_type_out)?;
        cell.set_item("style_id", style_id)?;
        Ok(cell)
    }

    /// A row (opened, counted unless `empty`): `("row", number, extra attributes or None, cells)`.
    fn row<'py>(
        &mut self,
        py: Python<'py>,
        start: &BytesStart,
        empty: bool,
    ) -> PyResult<Bound<'py, PyTuple>> {
        if !empty {
            self.depth += 1;
        }
        let attrs = match attributes(
            start,
            if empty { self.depth + 1 } else { self.depth },
            &mut self.scope,
            false,
        ) {
            Some(attrs) => attrs,
            None => return handed_back(),
        };
        if empty {
            self.scope.close(self.depth + 1);
        }
        self.row_number = match attrs.get(b"r") {
            Some(number) if ascii_digits(number) => match number.parse::<i64>() {
                Ok(number) => number,
                Err(_) => return handed_back(),
            },
            Some(_) => return handed_back(), // a row number openpyxl reads otherwise
            None => self.row_number + 1,
        };
        let extra = if attrs.pairs.iter().any(|(key, _)| {
            !key.contains(&b':') && key != b"xmlns" && key != b"r" && key != b"spans"
        }) {
            let extra = PyDict::new(py);
            for (key, value) in &attrs.pairs {
                if !key.starts_with(b"xmlns") {
                    extra.set_item(
                        PyString::new(py, std::str::from_utf8(key).unwrap_or_default()),
                        value,
                    )?;
                }
            }
            extra.into_any()
        } else {
            py.None().into_bound(py)
        };
        let cells = PyList::empty(py);
        let mut column_counter = 0usize;
        if !empty {
            loop {
                match self.event(false)? {
                    Event::Start(start) => {
                        if start.name().as_ref().as_bytes() != b"c" {
                            return handed_back();
                        }
                        cells.append(self.cell(py, &start, false, &mut column_counter)?)?;
                    }
                    Event::Empty(start) => {
                        if start.name().as_ref().as_bytes() != b"c" {
                            return handed_back();
                        }
                        cells.append(self.cell(py, &start, true, &mut column_counter)?)?;
                    }
                    Event::End(_) => {
                        self.scope.close(self.depth);
                        self.depth -= 1;
                        break;
                    }
                    Event::Text(_) | Event::CData(_) | Event::GeneralRef(_) => {}
                    _ => return handed_back(),
                }
            }
        }
        PyTuple::new(
            py,
            [
                PyString::new(py, "row").into_any(),
                self.row_number.into_pyobject(py)?.into_any(),
                extra,
                cells.into_any(),
            ],
        )
    }

    /// A `<mergeCells>` (opened, counted unless `empty`): `("merges", [ref of each mergeCell child, "" for any other])`.
    fn merges<'py>(&mut self, py: Python<'py>, empty: bool) -> PyResult<Bound<'py, PyTuple>> {
        if !empty {
            self.depth += 1;
        }
        let references = PyList::empty(py);
        if !empty {
            let floor = self.depth;
            loop {
                let event = self.sheet_level_event(false)?;
                if let Event::Start(start) | Event::Empty(start) = &event {
                    let qname = start.name();
                    let name = qname.as_ref().as_bytes();
                    if sheet_element(name) && !(name == b"mergeCell" && self.depth == floor) {
                        return handed_back();
                    }
                }
                let opened = match &event {
                    Event::Start(start) => Some((start.clone(), true)),
                    Event::Empty(start) => Some((start.clone(), false)),
                    _ => None,
                };
                match event {
                    Event::Start(_) | Event::Empty(_) => {
                        let (start, opens) = opened.expect("a start tag");
                        let at = self.depth + 1;
                        let attrs = match attributes(&start, at, &mut self.scope, false) {
                            Some(attrs) => attrs,
                            None => return handed_back(),
                        };
                        if self.depth == floor {
                            let reference = if start.name().as_ref().as_bytes() == b"mergeCell" {
                                attrs.get(b"ref").unwrap_or("")
                            } else {
                                ""
                            };
                            references.append(reference)?;
                        }
                        if opens {
                            self.depth += 1;
                        } else {
                            self.scope.close(at);
                        }
                    }
                    Event::End(_) => {
                        self.scope.close(self.depth);
                        self.depth -= 1;
                        if self.depth < floor {
                            break;
                        }
                    }
                    Event::Text(_)
                    | Event::CData(_)
                    | Event::GeneralRef(_)
                    | Event::Comment(_)
                    | Event::PI(_) => {}
                    _ => return handed_back(),
                }
            }
        }
        PyTuple::new(
            py,
            [
                PyString::new(py, "merges").into_any(),
                references.into_any(),
            ],
        )
    }
}

#[pymethods]
impl SheetRows {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyTuple>>> {
        if self.done {
            return Ok(None);
        }
        loop {
            let event = self.sheet_level_event(false)?;
            let opened = match &event {
                Event::Start(start) => Some((start.clone(), false)),
                Event::Empty(start) => Some((start.clone(), true)),
                _ => None,
            };
            match event {
                Event::Start(_) | Event::Empty(_) => {
                    let (start, empty) = opened.expect("a start tag");
                    let name = start.name().as_ref().as_bytes().to_vec();
                    if !self.root_seen {
                        self.root_seen = true;
                        if attributes(&start, 1, &mut self.scope, true).is_none()
                            || name.contains(&b':')
                        {
                            return handed_back();
                        }
                        if empty {
                            self.root_closed = true;
                        } else {
                            self.depth = 1;
                        }
                        continue;
                    }
                    if self.in_sheet_data && self.depth == 2 && name == b"row" {
                        return Ok(Some(self.row(py, &start, empty)?));
                    }
                    if self.depth == 1 && name == b"mergeCells" {
                        return Ok(Some(self.merges(py, empty)?));
                    }
                    if self.in_sheet_data
                        || (sheet_element(&name) && !(self.depth == 1 && name == b"sheetData"))
                    {
                        return handed_back(); // something else in the sheet data, or a sheet element out of its place
                    }
                    if self.depth == 1 && name == b"sheetData" {
                        if attributes(&start, 2, &mut self.scope, false).is_none() {
                            return handed_back();
                        }
                        if empty {
                            self.scope.close(2);
                        } else {
                            self.depth = 2;
                            self.in_sheet_data = true;
                        }
                        continue;
                    }
                    if empty {
                        if attributes(&start, self.depth + 1, &mut self.scope, false).is_none() {
                            return handed_back();
                        }
                        self.scope.close(self.depth + 1);
                    } else {
                        self.depth += 1;
                        if attributes(&start, self.depth, &mut self.scope, false).is_none() {
                            return handed_back();
                        }
                        self.skip_element()?;
                    }
                }
                Event::End(_) => {
                    self.scope.close(self.depth);
                    if self.in_sheet_data && self.depth == 2 {
                        self.in_sheet_data = false;
                    }
                    self.depth -= 1;
                    if self.depth == 0 {
                        self.root_closed = true;
                    }
                }
                Event::Eof => {
                    self.done = true;
                    return Ok(None);
                }
                _ => {}
            }
        }
    }
}

/// The rows of a sheet part, read from the file object `source` (left at its start by the caller).
#[pyfunction]
#[pyo3(signature = (source, shared_strings, style_count, date_formats, timedelta_formats, from_excel, epoch, rich_text))]
#[allow(clippy::too_many_arguments)]
fn sheet_rows(
    source: Py<PyAny>,
    shared_strings: Py<PyList>,
    style_count: Option<usize>,
    date_formats: Py<PyAny>,
    timedelta_formats: Py<PyAny>,
    from_excel: Py<PyAny>,
    epoch: Py<PyAny>,
    rich_text: bool,
) -> SheetRows {
    let mut reader = Reader::from_reader(BufReader::with_capacity(256 * 1024, PySource { source }));
    let config = reader.config_mut();
    config.check_end_names = true;
    config.check_comments = true;
    config.expand_empty_elements = false;
    config.trim_text(false);
    SheetRows {
        reader,
        context: Context {
            shared_strings,
            style_count,
            date_formats,
            timedelta_formats,
            from_excel,
            epoch,
            rich_text,
        },
        scope: Scope {
            declared: Vec::new(),
        },
        depth: 0,
        in_sheet_data: false,
        root_seen: false,
        root_closed: false,
        row_number: 0,
        buf: Vec::with_capacity(4096),
        inner: Vec::with_capacity(4096),
        done: false,
    }
}

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("HandedBack", module.py().get_type::<HandedBack>())?;
    module.add_function(wrap_pyfunction!(sheet_rows, module)?)?;
    Ok(())
}
