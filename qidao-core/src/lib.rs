uniffi::setup_scaffolding!();

use sgf_parse::{
    go::{parse, Prop},
    SgfNode as ParserNode, SgfProp,
};
use std::sync::{Arc, Mutex, OnceLock};
use thiserror::Error;
use tokio::runtime::Runtime;

pub mod engine;

static RUNTIME: OnceLock<Runtime> = OnceLock::new();

const MAX_SGF_BYTES: usize = 8 * 1024 * 1024;
const MAX_SGF_NODES: usize = 10_000;
const MAX_SGF_PATH_DEPTH: usize = 1024;
const MAX_SGF_STRUCTURE_DEPTH: usize = 128;

fn get_runtime() -> &'static Runtime {
    RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("Failed to create Tokio runtime")
    })
}

#[derive(uniffi::Record, Clone, Default)]
pub struct GameMetadata {
    pub black_name: String,
    pub black_rank: String,
    pub white_name: String,
    pub white_rank: String,
    pub komi: f64,
    pub handicap: u32,
    pub result: String,
    pub date: String,
    pub event: String,
    pub game_name: String,
    pub place: String,
    pub size: u32,
}

#[derive(uniffi::Record, Clone)]
pub struct SgfProperty {
    pub identifier: String,
    pub values: Vec<String>,
}

#[uniffi::export]
pub fn add(a: u32, b: u32) -> u32 {
    a + b
}

#[derive(Debug, Error, uniffi::Error)]
pub enum SgfError {
    #[error("Parse error: {message}")]
    ParseError { message: String },
    #[error("Invalid move: {message}")]
    InvalidMove { message: String },
}

fn validate_board_size(size: u32) -> Result<u32, SgfError> {
    match size {
        9 | 13 | 19 => Ok(size),
        _ => Err(SgfError::ParseError {
            message: "QiDao supports square board sizes 9, 13, or 19".into(),
        }),
    }
}

fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn validate_sgf_source(sgf: &str) -> Result<(), SgfError> {
    if sgf.len() > MAX_SGF_BYTES {
        return Err(SgfError::ParseError {
            message: format!("SGF exceeds byte limit of {MAX_SGF_BYTES}"),
        });
    }

    let mut in_property = false;
    let mut escaped = false;
    let mut node_count = 0usize;
    let mut path_depth = 0usize;
    let mut variation_paths = Vec::new();

    for byte in sgf.bytes() {
        if in_property {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b']' {
                in_property = false;
            }
            continue;
        }

        match byte {
            b'[' => in_property = true,
            b'(' => {
                variation_paths.push(path_depth);
                if variation_paths.len() > MAX_SGF_STRUCTURE_DEPTH {
                    return Err(SgfError::ParseError {
                        message: format!(
                            "SGF exceeds structural depth limit of {MAX_SGF_STRUCTURE_DEPTH}"
                        ),
                    });
                }
            }
            b')' => {
                if let Some(parent_depth) = variation_paths.pop() {
                    path_depth = parent_depth;
                }
            }
            b';' => {
                node_count += 1;
                if node_count > MAX_SGF_NODES {
                    return Err(SgfError::ParseError {
                        message: format!("SGF exceeds node limit of {MAX_SGF_NODES}"),
                    });
                }
                path_depth += 1;
                if path_depth > MAX_SGF_PATH_DEPTH {
                    return Err(SgfError::ParseError {
                        message: format!("SGF exceeds path depth limit of {MAX_SGF_PATH_DEPTH}"),
                    });
                }
            }
            _ => {}
        }
    }

    Ok(())
}

fn validate_parsed_tree_bounds(root: &ParserNode<Prop>) -> Result<(), SgfError> {
    let mut node_count = 0usize;
    let mut stack = vec![(root, 1usize)];

    while let Some((node, depth)) = stack.pop() {
        node_count += 1;
        if node_count > MAX_SGF_NODES {
            return Err(SgfError::ParseError {
                message: format!("SGF exceeds node limit of {MAX_SGF_NODES}"),
            });
        }
        if depth > MAX_SGF_PATH_DEPTH {
            return Err(SgfError::ParseError {
                message: format!("SGF exceeds path depth limit of {MAX_SGF_PATH_DEPTH}"),
            });
        }
        stack.extend(node.children().map(|child| (child, depth + 1)));
    }

    Ok(())
}

fn validate_parsed_tree(root: &ParserNode<Prop>) -> Result<(), SgfError> {
    validate_parsed_tree_bounds(root)?;
    if root
        .properties()
        .filter(|property| property.identifier() == "SZ")
        .count()
        > 1
    {
        return Err(SgfError::ParseError {
            message: "SGF contains duplicate root SZ properties".into(),
        });
    }
    Ok(())
}

#[derive(uniffi::Object)]
pub struct SgfNode {
    pub properties: Mutex<Vec<SgfProperty>>,
    pub children: Mutex<Vec<Arc<SgfNode>>>,
}

#[uniffi::export]
impl SgfNode {
    pub fn get_id(&self) -> String {
        format!("{:p}", self)
    }

    pub fn get_properties(&self) -> Vec<SgfProperty> {
        self.properties.lock().unwrap().clone()
    }

    pub fn get_children(&self) -> Vec<Arc<SgfNode>> {
        self.children.lock().unwrap().clone()
    }
}

#[derive(uniffi::Object)]
pub struct SgfTree {
    pub root: Arc<SgfNode>,
}

#[uniffi::export]
impl SgfTree {
    pub fn root(&self) -> Arc<SgfNode> {
        self.root.clone()
    }
}

// --- Go Rules Engine ---

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum StoneColor {
    Black,
    White,
}

impl StoneColor {
    pub fn opponent(&self) -> Self {
        match self {
            Self::Black => Self::White,
            Self::White => Self::Black,
        }
    }
}

#[derive(uniffi::Object)]
pub struct Board {
    size: u32,
    grid: Vec<Option<StoneColor>>,         // Flat array for performance
    last_captured_pos: Option<(u32, u32)>, // Simple Ko support
}

#[uniffi::export]
impl Board {
    #[uniffi::constructor]
    pub fn new(size: u32) -> Result<Arc<Self>, SgfError> {
        let size = validate_board_size(size)?;
        Ok(Self::trusted_empty(size))
    }

    pub fn get_size(&self) -> u32 {
        self.size
    }

    pub fn get_stone(&self, x: u32, y: u32) -> Option<StoneColor> {
        if x >= self.size || y >= self.size {
            return None;
        }
        self.grid[(y * self.size + x) as usize]
    }

    /// Forcefully places a stone without checking rules (for setup stones).
    pub fn with_stone(&self, x: u32, y: u32, color: Option<StoneColor>) -> Arc<Board> {
        if x >= self.size || y >= self.size {
            return Arc::new(Board {
                size: self.size,
                grid: self.grid.clone(),
                last_captured_pos: self.last_captured_pos,
            });
        }
        let mut new_grid = self.grid.clone();
        new_grid[(y * self.size + x) as usize] = color;
        Arc::new(Board {
            size: self.size,
            grid: new_grid,
            last_captured_pos: self.last_captured_pos,
        })
    }

    /// Attempts to place a stone. Returns true if successful.
    pub fn place_stone(&self, x: u32, y: u32, color: StoneColor) -> Result<Arc<Board>, SgfError> {
        if x >= self.size || y >= self.size {
            return Err(SgfError::ParseError {
                message: "Out of bounds".into(),
            });
        }
        if self.get_stone(x, y).is_some() {
            return Err(SgfError::ParseError {
                message: "Position occupied".into(),
            });
        }

        let mut new_grid = self.grid.clone();
        let idx = (y * self.size + x) as usize;
        new_grid[idx] = Some(color);

        // 1. Check for captures of opponent
        let opponent = color.opponent();
        let mut captured_any = false;
        let mut last_cap = None;
        let mut capture_count = 0;

        for (nx, ny) in self.neighbors(x, y) {
            if new_grid[(ny * self.size + nx) as usize] == Some(opponent) {
                if self.count_liberties(&new_grid, nx, ny) == 0 {
                    let captured = self.get_group(&new_grid, nx, ny);
                    capture_count += captured.len();
                    for (cx, cy) in &captured {
                        new_grid[(cy * self.size + cx) as usize] = None;
                        last_cap = Some((*cx, *cy));
                    }
                    captured_any = true;
                }
            }
        }

        // 2. Check for suicide (unless it captures)
        if !captured_any && self.count_liberties(&new_grid, x, y) == 0 {
            return Err(SgfError::ParseError {
                message: "Suicide move".into(),
            });
        }

        // 3. Simple Ko check (if exactly one stone was captured)
        if capture_count == 1 {
            if let Some(pos) = self.last_captured_pos {
                if pos == (x, y) {
                    return Err(SgfError::ParseError {
                        message: "Ko violation".into(),
                    });
                }
            }
        } else {
            last_cap = None;
        }

        Ok(Arc::new(Board {
            size: self.size,
            grid: new_grid,
            last_captured_pos: last_cap,
        }))
    }
}

impl Board {
    fn trusted_empty(size: u32) -> Arc<Self> {
        Arc::new(Self {
            size,
            grid: vec![None; (size * size) as usize],
            last_captured_pos: None,
        })
    }
    fn neighbors(&self, x: u32, y: u32) -> Vec<(u32, u32)> {
        let mut n = Vec::new();
        if x > 0 {
            n.push((x - 1, y));
        }
        if x < self.size - 1 {
            n.push((x + 1, y));
        }
        if y > 0 {
            n.push((x, y - 1));
        }
        if y < self.size - 1 {
            n.push((x, y + 1));
        }
        n
    }

    fn get_group(&self, grid: &[Option<StoneColor>], x: u32, y: u32) -> Vec<(u32, u32)> {
        let color = grid[(y * self.size + x) as usize];
        let mut group = Vec::new();
        let mut stack = vec![(x, y)];
        let mut visited = std::collections::HashSet::new();

        while let Some((cx, cy)) = stack.pop() {
            if !visited.insert((cx, cy)) {
                continue;
            }
            if grid[(cy * self.size + cx) as usize] == color {
                group.push((cx, cy));
                for (nx, ny) in self.neighbors(cx, cy) {
                    stack.push((nx, ny));
                }
            }
        }
        group
    }

    fn count_liberties(&self, grid: &[Option<StoneColor>], x: u32, y: u32) -> u32 {
        let color = grid[(y * self.size + x) as usize];
        let mut liberties = std::collections::HashSet::new();
        let mut stack = vec![(x, y)];
        let mut visited = std::collections::HashSet::new();

        while let Some((cx, cy)) = stack.pop() {
            if !visited.insert((cx, cy)) {
                continue;
            }
            if grid[(cy * self.size + cx) as usize] == color {
                for (nx, ny) in self.neighbors(cx, cy) {
                    stack.push((nx, ny));
                }
            } else if grid[(cy * self.size + cx) as usize].is_none() {
                liberties.insert((cx, cy));
            }
        }
        liberties.len() as u32
    }
}

fn sgf_to_gtp(sgf_coord: &str, size: u32) -> String {
    if sgf_coord.is_empty() {
        return "pass".to_string();
    }
    let chars: Vec<char> = sgf_coord.chars().collect();
    if chars.len() < 2 {
        return "pass".to_string();
    }

    let x = (chars[0] as u32).saturating_sub('a' as u32);
    let y = (chars[1] as u32).saturating_sub('a' as u32);

    if x >= size || y >= size {
        return "pass".to_string();
    }

    let col_char = if x < 8 {
        (b'A' + x as u8) as char
    } else {
        (b'A' + x as u8 + 1) as char // Skip 'I'
    };

    let row = size - y;
    format!("{}{}", col_char, row)
}

fn convert_node(node: &ParserNode<Prop>) -> Arc<SgfNode> {
    let properties = node
        .properties()
        .map(|prop: &Prop| {
            let id = prop.identifier();
            let mut values = Vec::new();

            match prop {
                Prop::SZ(size) => {
                    if size.0 == size.1 {
                        values.push(size.0.to_string());
                    } else {
                        values.push(format!("{}:{}", size.0, size.1));
                    }
                }
                _ => {
                    let s = prop.to_string();
                    let mut current_value = String::new();
                    let mut in_brackets = false;
                    let mut escaped = false;

                    // s is "ID[val1][val2]"
                    for c in s.chars().skip(id.len()) {
                        if escaped {
                            current_value.push(c);
                            escaped = false;
                        } else if c == '\\' {
                            escaped = true;
                        } else if c == '[' {
                            if in_brackets {
                                current_value.push(c);
                            } else {
                                in_brackets = true;
                            }
                        } else if c == ']' {
                            if escaped {
                                current_value.push(c);
                                escaped = false;
                            } else {
                                values.push(current_value.clone());
                                current_value.clear();
                                in_brackets = false;
                            }
                        } else {
                            current_value.push(c);
                        }
                    }
                }
            }

            SgfProperty {
                identifier: id,
                values,
            }
        })
        .collect();

    let children = node.children().map(|c| convert_node(c)).collect();

    Arc::new(SgfNode {
        properties: Mutex::new(properties),
        children: Mutex::new(children),
    })
}

fn serialize_node(node: &Arc<SgfNode>, out: &mut String) {
    out.push(';');
    let props = node.properties.lock().unwrap();
    for prop in props.iter() {
        if prop.values.is_empty() {
            continue;
        }
        out.push_str(&prop.identifier);
        for val in &prop.values {
            out.push('[');
            // Basic escaping
            let escaped = val.replace('\\', "\\\\").replace(']', "\\]");
            out.push_str(&escaped);
            out.push(']');
        }
    }

    let children = node.children.lock().unwrap();
    if children.len() == 1 {
        serialize_node(&children[0], out);
    } else {
        for child in children.iter() {
            out.push('(');
            serialize_node(child, out);
            out.push(')');
        }
    }
}

#[uniffi::export]
pub fn parse_sgf(sgf_content: String) -> Result<Arc<SgfTree>, SgfError> {
    validate_sgf_source(&sgf_content)?;
    let trimmed = sgf_content.trim().trim_matches('\0').trim().to_string();
    if trimmed.is_empty() {
        return Err(SgfError::ParseError {
            message: "Empty SGF content".to_string(),
        });
    }

    // Try parsing normally first
    match parse(&trimmed) {
        Ok(trees) => {
            if let Some(first_tree) = trees.iter().next() {
                validate_parsed_tree(first_tree)?;
                Ok(Arc::new(SgfTree {
                    root: convert_node(first_tree),
                }))
            } else {
                Err(SgfError::ParseError {
                    message: "No tree found in SGF".to_string(),
                })
            }
        }
        Err(e) => {
            // If it fails, try to "fix" it if it looks truncated
            // This is a common issue with some SGF sources

            // Try different combinations of closing brackets and parentheses
            for brackets in 0..3 {
                let mut base = trimmed.clone();
                for _ in 0..brackets {
                    base.push(']');
                }

                for parens in 1..10 {
                    let mut attempt = base.clone();
                    for _ in 0..parens {
                        attempt.push(')');
                    }

                    validate_sgf_source(&attempt)?;
                    if let Ok(trees) = parse(&attempt) {
                        if let Some(first_tree) = trees.iter().next() {
                            validate_parsed_tree(first_tree)?;
                            return Ok(Arc::new(SgfTree {
                                root: convert_node(first_tree),
                            }));
                        }
                    }
                }
            }

            Err(SgfError::ParseError {
                message: e.to_string(),
            })
        }
    }
}

struct GameState {
    root: Arc<SgfNode>,
    current_node: Arc<SgfNode>,
    history: Vec<Arc<SgfNode>>,
    board_cache: std::collections::HashMap<usize, Arc<Board>>,
    size: u32,
}

#[derive(uniffi::Object)]
pub struct Game {
    state: Mutex<GameState>,
}

#[uniffi::export]
impl Game {
    #[uniffi::constructor]
    pub fn new(size: u32) -> Result<Arc<Self>, SgfError> {
        let size = validate_board_size(size)?;
        let root = Arc::new(SgfNode {
            properties: Mutex::new(vec![SgfProperty {
                identifier: "SZ".to_string(),
                values: vec![size.to_string()],
            }]),
            children: Mutex::new(vec![]),
        });

        let mut board_cache = std::collections::HashMap::new();
        board_cache.insert(Arc::as_ptr(&root) as usize, Board::trusted_empty(size));

        Ok(Arc::new(Self {
            state: Mutex::new(GameState {
                root: root.clone(),
                current_node: root,
                history: vec![],
                board_cache,
                size,
            }),
        }))
    }

    #[uniffi::constructor]
    pub fn from_sgf(sgf_content: String) -> Result<Arc<Self>, SgfError> {
        let tree = parse_sgf(sgf_content)?;
        let root = tree.root();

        let size = {
            let props = root.properties.lock().unwrap();
            match props
                .iter()
                .find(|p| p.identifier == "SZ")
                .and_then(|p| p.values.first())
            {
                Some(value) => value.parse::<u32>().map_err(|_| SgfError::ParseError {
                    message: "QiDao supports square board sizes 9, 13, or 19".into(),
                })?,
                None => 19,
            }
        };
        let size = validate_board_size(size)?;

        let board_cache = std::collections::HashMap::new();

        Ok(Arc::new(Self {
            state: Mutex::new(GameState {
                root: root.clone(),
                current_node: root,
                history: vec![],
                board_cache,
                size,
            }),
        }))
    }

    pub fn get_metadata(&self) -> GameMetadata {
        let state = self.state.lock().unwrap();
        let props = state.root.properties.lock().unwrap();

        let mut meta = GameMetadata::default();
        meta.size = state.size;

        for p in props.iter() {
            match p.identifier.as_str() {
                "PB" => meta.black_name = p.values.first().cloned().unwrap_or_default(),
                "BR" => meta.black_rank = p.values.first().cloned().unwrap_or_default(),
                "PW" => meta.white_name = p.values.first().cloned().unwrap_or_default(),
                "WR" => meta.white_rank = p.values.first().cloned().unwrap_or_default(),
                "KM" => meta.komi = p.values.first().and_then(|v| v.parse().ok()).unwrap_or(0.0),
                "HA" => meta.handicap = p.values.first().and_then(|v| v.parse().ok()).unwrap_or(0),
                "RE" => meta.result = p.values.first().cloned().unwrap_or_default(),
                "DT" => meta.date = p.values.first().cloned().unwrap_or_default(),
                "EV" => meta.event = p.values.first().cloned().unwrap_or_default(),
                "GN" => meta.game_name = p.values.first().cloned().unwrap_or_default(),
                "PC" => meta.place = p.values.first().cloned().unwrap_or_default(),
                _ => {}
            }
        }
        meta
    }

    pub fn get_comment(&self) -> String {
        let state = self.state.lock().unwrap();
        let props = state.current_node.properties.lock().unwrap();
        if let Some(prop) = props.iter().find(|p| p.identifier == "C") {
            if let Some(val) = prop.values.first() {
                return val.clone();
            }
        }
        "".to_string()
    }

    pub fn set_comment(&self, comment: String) {
        let state = self.state.lock().unwrap();
        let mut props = state.current_node.properties.lock().unwrap();
        if let Some(prop) = props.iter_mut().find(|p| p.identifier == "C") {
            prop.values = vec![comment];
        } else {
            props.push(SgfProperty {
                identifier: "C".to_string(),
                values: vec![comment],
            });
        }
    }

    pub fn get_current_node(&self) -> Arc<SgfNode> {
        self.state.lock().unwrap().current_node.clone()
    }

    pub fn get_root_node(&self) -> Arc<SgfNode> {
        self.state.lock().unwrap().root.clone()
    }

    pub fn get_current_variation_index(&self) -> u32 {
        let state = self.state.lock().unwrap();
        if let Some(parent) = state.history.last() {
            let children = parent.children.lock().unwrap();
            for (i, child) in children.iter().enumerate() {
                if Arc::ptr_eq(child, &state.current_node) {
                    return i as u32;
                }
            }
        }
        0
    }

    pub fn get_variation_count(&self) -> u32 {
        let state = self.state.lock().unwrap();
        if let Some(parent) = state.history.last() {
            return parent.children.lock().unwrap().len() as u32;
        }
        1
    }

    pub fn set_metadata(&self, metadata: GameMetadata) {
        let state = self.state.lock().unwrap();
        let mut props = state.root.properties.lock().unwrap();

        let updates = [
            ("PB", metadata.black_name),
            ("BR", metadata.black_rank),
            ("PW", metadata.white_name),
            ("WR", metadata.white_rank),
            ("KM", metadata.komi.to_string()),
            ("HA", metadata.handicap.to_string()),
            ("RE", metadata.result),
            ("DT", metadata.date),
            ("EV", metadata.event),
            ("GN", metadata.game_name),
            ("PC", metadata.place),
            ("SZ", state.size.to_string()),
        ];

        for (id, val) in updates {
            if let Some(p) = props.iter_mut().find(|p| p.identifier == id) {
                p.values = vec![val];
            } else {
                props.push(SgfProperty {
                    identifier: id.to_string(),
                    values: vec![val],
                });
            }
        }
    }

    pub fn to_sgf(&self) -> String {
        let state = self.state.lock().unwrap();
        let mut out = String::from("(");
        serialize_node(&state.root, &mut out);
        out.push(')');
        out
    }

    pub fn get_current_state_sgf(&self) -> String {
        let board = self.get_board();
        let size = board.get_size();
        let mut out = String::from("(;");

        // 1. Size
        out.push_str(&format!("SZ[{}]", size));

        // 2. Stones
        let mut black_stones = Vec::new();
        let mut white_stones = Vec::new();
        for y in 0..size {
            for x in 0..size {
                if let Some(color) = board.get_stone(x, y) {
                    let coords =
                        format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);
                    match color {
                        StoneColor::Black => black_stones.push(coords),
                        StoneColor::White => white_stones.push(coords),
                    }
                }
            }
        }

        if !black_stones.is_empty() {
            out.push_str("AB");
            for s in black_stones {
                out.push('[');
                out.push_str(&s);
                out.push(']');
            }
        }
        if !white_stones.is_empty() {
            out.push_str("AW");
            for s in white_stones {
                out.push('[');
                out.push_str(&s);
                out.push(']');
            }
        }

        // 3. Other properties from current node (markers, labels, comments)
        let state = self.state.lock().unwrap();
        let props = state.current_node.properties.lock().unwrap();
        for prop in props.iter() {
            // Skip stone/move properties as we already handled them via board state
            if ["B", "W", "AB", "AW", "AE", "SZ"].contains(&prop.identifier.as_str()) {
                continue;
            }
            if prop.values.is_empty() {
                continue;
            }
            out.push_str(&prop.identifier);
            for val in &prop.values {
                out.push('[');
                let escaped = val.replace('\\', "\\\\").replace(']', "\\]");
                out.push_str(&escaped);
                out.push(']');
            }
        }

        out.push_str(")");
        out
    }

    pub fn jump_to_node(&self, target: Arc<SgfNode>) {
        let mut state = self.state.lock().unwrap();
        if let Some(path) = find_path(&state.root, &target) {
            state.history = path;
            state.current_node = target;
        }
    }

    pub fn jump_to_move_number(&self, target: u32) {
        let mut state = self.state.lock().unwrap();
        if let Some((node, path)) = find_node_at_depth(&state.root, target, vec![]) {
            state.current_node = node;
            state.history = path;
        }
    }

    pub fn get_board(&self) -> Arc<Board> {
        let mut state = self.state.lock().unwrap();
        Self::current_board_internal(&mut state)
    }

    pub fn get_move_count(&self) -> u32 {
        self.state.lock().unwrap().history.len() as u32
    }

    pub fn get_max_move_count(&self) -> u32 {
        let state = self.state.lock().unwrap();
        get_max_depth(&state.root)
    }

    pub fn set_next_player(&self, color: StoneColor) {
        let state = self.state.lock().unwrap();
        let mut props = state.current_node.properties.lock().unwrap();
        let val = match color {
            StoneColor::Black => "B",
            StoneColor::White => "W",
        };

        if let Some(p) = props.iter_mut().find(|p| p.identifier == "PL") {
            p.values = vec![val.to_string()];
        } else {
            props.push(SgfProperty {
                identifier: "PL".to_string(),
                values: vec![val.to_string()],
            });
        }
    }

    pub fn get_next_color(&self) -> StoneColor {
        let state = self.state.lock().unwrap();
        self.get_next_color_for_node(&state.current_node)
    }

    pub fn get_initial_color(&self) -> StoneColor {
        let state = self.state.lock().unwrap();
        self.get_next_color_for_node(&state.root)
    }

    fn get_next_color_for_node(&self, node: &Arc<SgfNode>) -> StoneColor {
        // 1. Check PL property in current node
        {
            let props = node.properties.lock().unwrap();
            if let Some(p) = props.iter().find(|p| p.identifier == "PL") {
                if let Some(v) = p.values.first() {
                    if v == "B" {
                        return StoneColor::Black;
                    }
                    if v == "W" {
                        return StoneColor::White;
                    }
                }
            }
        }

        // 2. Check if current node is a move
        {
            let props = node.properties.lock().unwrap();
            for prop in props.iter() {
                if prop.identifier == "B" {
                    return StoneColor::White;
                }
                if prop.identifier == "W" {
                    return StoneColor::Black;
                }
            }
        }

        // 3. Default to Black
        StoneColor::Black
    }

    pub fn get_last_move(&self) -> Option<SgfProperty> {
        let state = self.state.lock().unwrap();
        let props = state.current_node.properties.lock().unwrap();
        props
            .iter()
            .find(|p| p.identifier == "B" || p.identifier == "W")
            .cloned()
    }

    pub fn get_current_path_moves(&self) -> Vec<SgfProperty> {
        let state = self.state.lock().unwrap();
        let mut moves = Vec::new();

        // Add moves from history
        for node in &state.history {
            let props = node.properties.lock().unwrap();
            if let Some(prop) = props
                .iter()
                .find(|p| p.identifier == "B" || p.identifier == "W")
            {
                moves.push(prop.clone());
            }
        }

        // Add move from current node
        let props = state.current_node.properties.lock().unwrap();
        if let Some(prop) = props
            .iter()
            .find(|p| p.identifier == "B" || p.identifier == "W")
        {
            moves.push(prop.clone());
        }

        moves
    }

    pub fn get_initial_stones(&self) -> Vec<Vec<String>> {
        let state = self.state.lock().unwrap();
        let size = state.size;
        let mut stones = Vec::new();

        // Collect all AB/AW from the root node as initial stones
        let props = state.root.properties.lock().unwrap();
        for prop in props.iter() {
            if prop.identifier == "AB" || prop.identifier == "AW" {
                let color = if prop.identifier == "AB" { "B" } else { "W" };
                for val in &prop.values {
                    let gtp_move = sgf_to_gtp(val, size);
                    stones.push(vec![color.to_string(), gtp_move]);
                }
            }
        }
        stones
    }

    pub fn get_analysis_moves(&self) -> Vec<Vec<String>> {
        let state = self.state.lock().unwrap();
        let size = state.size;
        let mut path = state.history.clone();
        path.push(state.current_node.clone());

        let mut moves = Vec::new();
        for node in path {
            let props = node.properties.lock().unwrap();
            for prop in props.iter() {
                if prop.identifier == "B" || prop.identifier == "W" {
                    if let Some(val) = prop.values.first() {
                        let gtp_move = sgf_to_gtp(val, size);
                        moves.push(vec![prop.identifier.clone(), gtp_move]);
                    }
                }
            }
        }
        moves
    }

    pub fn get_current_board_stones(&self) -> Vec<Vec<String>> {
        let board = self.get_board();
        let size = board.get_size();
        let mut stones = Vec::new();
        for y in 0..size {
            for x in 0..size {
                if let Some(color) = board.get_stone(x, y) {
                    let color_str = match color {
                        StoneColor::Black => "B",
                        StoneColor::White => "W",
                    };
                    let col = if x >= 8 {
                        (b'A' + x as u8 + 1) as char
                    } else {
                        (b'A' + x as u8) as char
                    };
                    let row = size - y;
                    let gtp_move = format!("{}{}", col, row);
                    stones.push(vec![color_str.to_string(), gtp_move]);
                }
            }
        }
        stones
    }

    pub fn get_main_line_moves(&self) -> Vec<Vec<String>> {
        let state = self.state.lock().unwrap();
        let mut moves = Vec::new();
        let mut current = state.root.clone();
        let size = state.size;

        loop {
            let children = current.get_children();
            if children.is_empty() {
                break;
            }
            // Always follow the first child for the main line
            let next = children[0].clone();
            let props = next.get_properties();
            if let Some(move_prop) = props
                .iter()
                .find(|p| p.identifier == "B" || p.identifier == "W")
            {
                if let Some(coords) = move_prop.values.first() {
                    let color = move_prop.identifier.clone();
                    let gtp_move = sgf_to_gtp(coords, size);
                    moves.push(vec![color, gtp_move]);
                }
            }
            current = next;
        }
        moves
    }

    pub fn can_go_back(&self) -> bool {
        !self.state.lock().unwrap().history.is_empty()
    }

    pub fn can_go_forward(&self) -> bool {
        !self
            .state
            .lock()
            .unwrap()
            .current_node
            .children
            .lock()
            .unwrap()
            .is_empty()
    }

    pub fn go_back(&self) -> bool {
        let mut state = self.state.lock().unwrap();
        if let Some(prev) = state.history.pop() {
            state.current_node = prev;
            true
        } else {
            false
        }
    }

    pub fn go_forward(&self, index: u32) -> bool {
        let mut state = self.state.lock().unwrap();
        let children = state.current_node.children.lock().unwrap();
        let child = children.get(index as usize).cloned();
        drop(children); // Release lock before mutating state

        if let Some(child) = child {
            let current = state.current_node.clone();
            state.history.push(current);
            state.current_node = child;
            true
        } else {
            false
        }
    }

    pub fn delete_current_branch(&self) -> bool {
        let mut state = self.state.lock().unwrap();

        // Cannot delete the root node
        if state.history.is_empty() {
            return false;
        }

        let current_node = state.current_node.clone();
        let parent_node = state.history.pop().expect("History should not be empty");

        // Remove current_node from parent's children
        {
            let mut children = parent_node.children.lock().unwrap();
            children.retain(|c| !Arc::ptr_eq(c, &current_node));
        }

        // Move current_node back to parent
        state.current_node = parent_node;

        // Note: We don't explicitly clear board_cache for the deleted branch
        // as it's a HashMap and will just keep those entries until the Game is dropped.
        // In a very large SGF with many deletions, this might be a small leak,
        // but for now it's safe and simple.

        true
    }

    pub fn place_stone(&self, x: u32, y: u32, color: StoneColor) -> Result<(), SgfError> {
        let mut state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return Err(SgfError::InvalidMove {
                message: "Out of bounds".into(),
            });
        }

        // 1. Check if this move already exists as a child
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);
        let prop_id = match color {
            StoneColor::Black => "B",
            StoneColor::White => "W",
        };

        let existing_child = {
            let children = state.current_node.children.lock().unwrap();
            children
                .iter()
                .find(|c| {
                    let props = c.properties.lock().unwrap();
                    props
                        .iter()
                        .any(|p| p.identifier == prop_id && p.values.contains(&coords))
                })
                .cloned()
        };

        if let Some(child) = existing_child {
            // Move to existing child
            let current = state.current_node.clone();
            state.history.push(current);
            state.current_node = child;
            return Ok(());
        }

        // 2. Create new move
        let current_board = Self::current_board_internal(&mut state);

        let new_board = current_board.place_stone(x, y, color)?;

        let new_node = Arc::new(SgfNode {
            properties: Mutex::new(vec![SgfProperty {
                identifier: prop_id.to_string(),
                values: vec![coords],
            }]),
            children: Mutex::new(vec![]),
        });

        // Attach to tree
        state
            .current_node
            .children
            .lock()
            .unwrap()
            .push(new_node.clone());

        // Update state
        let current = state.current_node.clone();
        state.history.push(current);
        state.current_node = new_node.clone();
        state
            .board_cache
            .insert(Arc::as_ptr(&new_node) as usize, new_board);

        Ok(())
    }

    pub fn pass(&self, color: StoneColor) -> Result<(), SgfError> {
        let mut state = self.state.lock().unwrap();
        let current_board = Self::current_board_internal(&mut state);
        let prop_id = match color {
            StoneColor::Black => "B",
            StoneColor::White => "W",
        };

        let new_node = Arc::new(SgfNode {
            properties: Mutex::new(vec![SgfProperty {
                identifier: prop_id.to_string(),
                values: vec!["".to_string()],
            }]),
            children: Mutex::new(vec![]),
        });

        state
            .current_node
            .children
            .lock()
            .unwrap()
            .push(new_node.clone());
        let current = state.current_node.clone();
        state.history.push(current);
        state.current_node = new_node.clone();

        state
            .board_cache
            .insert(Arc::as_ptr(&new_node) as usize, current_board);

        Ok(())
    }

    pub fn add_stone(&self, x: u32, y: u32, color: StoneColor) {
        let mut state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return;
        }
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);
        let prop_id = match color {
            StoneColor::Black => "AB",
            StoneColor::White => "AW",
        };

        {
            let mut props = state.current_node.properties.lock().unwrap();
            // Remove from other stone properties first
            for id in &["AB", "AW", "AE"] {
                if let Some(p) = props.iter_mut().find(|p| p.identifier == *id) {
                    p.values.retain(|v| v != &coords);
                }
            }
            props.retain(|p| {
                !p.values.is_empty() || !["AB", "AW", "AE"].contains(&p.identifier.as_str())
            });

            if let Some(p) = props.iter_mut().find(|p| p.identifier == prop_id) {
                if !p.values.contains(&coords) {
                    p.values.push(coords);
                }
            } else {
                props.push(SgfProperty {
                    identifier: prop_id.to_string(),
                    values: vec![coords],
                });
            }
        }

        // Update board cache for this node
        self.recalculate_board_internal(&mut state);
    }

    pub fn remove_stone(&self, x: u32, y: u32) {
        let mut state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return;
        }
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);

        {
            let mut props = state.current_node.properties.lock().unwrap();
            // Remove from AB, AW
            for id in &["AB", "AW"] {
                if let Some(p) = props.iter_mut().find(|p| p.identifier == *id) {
                    p.values.retain(|v| v != &coords);
                }
            }
            props
                .retain(|p| !p.values.is_empty() || !["AB", "AW"].contains(&p.identifier.as_str()));

            // Add to AE (Add Empty)
            if let Some(p) = props.iter_mut().find(|p| p.identifier == "AE") {
                if !p.values.contains(&coords) {
                    p.values.push(coords);
                }
            } else {
                props.push(SgfProperty {
                    identifier: "AE".to_string(),
                    values: vec![coords],
                });
            }
        }

        // Update board cache for this node
        self.recalculate_board_internal(&mut state);
    }
}

impl Game {
    fn recalculate_board_internal(&self, state: &mut GameState) {
        state.board_cache.clear();
        Self::current_board_internal(state);
    }

    fn current_board_internal(state: &mut GameState) -> Arc<Board> {
        let current_ptr = Arc::as_ptr(&state.current_node) as usize;
        if let Some(board) = state.board_cache.get(&current_ptr) {
            return board.clone();
        }

        let mut path = state.history.clone();
        path.push(state.current_node.clone());

        let mut current_board = Board::trusted_empty(state.size);
        for node in path {
            let node_ptr = Arc::as_ptr(&node) as usize;
            if let Some(cached) = state.board_cache.get(&node_ptr) {
                current_board = cached.clone();
                continue;
            }

            let props = node.properties.lock().unwrap();
            for prop in props.iter() {
                match prop.identifier.as_str() {
                    "AB" | "AW" | "AE" => {
                        let color = if prop.identifier == "AB" {
                            Some(StoneColor::Black)
                        } else if prop.identifier == "AW" {
                            Some(StoneColor::White)
                        } else {
                            None
                        };
                        for coords in &prop.values {
                            if coords.len() == 2 {
                                let x = coords.as_bytes()[0] as i32 - 'a' as i32;
                                let y = coords.as_bytes()[1] as i32 - 'a' as i32;
                                current_board = current_board.with_stone(x as u32, y as u32, color);
                            }
                        }
                    }
                    _ => {}
                }
            }
            for prop in props.iter() {
                match prop.identifier.as_str() {
                    "B" | "W" => {
                        let color = if prop.identifier == "B" {
                            StoneColor::Black
                        } else {
                            StoneColor::White
                        };
                        if let Some(coords) = prop.values.first() {
                            if coords.len() == 2 {
                                let x = coords.as_bytes()[0] as i32 - 'a' as i32;
                                let y = coords.as_bytes()[1] as i32 - 'a' as i32;
                                if let Ok(next_board) =
                                    current_board.place_stone(x as u32, y as u32, color)
                                {
                                    current_board = next_board;
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
            state.board_cache.insert(node_ptr, current_board.clone());
        }

        current_board
    }
}

#[uniffi::export]
impl Game {
    pub fn add_mark(&self, x: u32, y: u32, mark_type: String) {
        let state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return;
        }
        let mut props = state.current_node.properties.lock().unwrap();
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);

        // mark_type should be TR, CR, SQ, MA
        if let Some(p) = props.iter_mut().find(|p| p.identifier == mark_type) {
            if !p.values.contains(&coords) {
                p.values.push(coords);
            }
        } else {
            props.push(SgfProperty {
                identifier: mark_type,
                values: vec![coords],
            });
        }
    }

    pub fn add_label(&self, x: u32, y: u32, label: String) {
        let state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return;
        }
        let mut props = state.current_node.properties.lock().unwrap();
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);
        let val = format!("{}:{}", coords, label);

        if let Some(p) = props.iter_mut().find(|p| p.identifier == "LB") {
            // Remove existing label at this coordinate
            p.values.retain(|v| !v.starts_with(&format!("{}:", coords)));
            p.values.push(val);
        } else {
            props.push(SgfProperty {
                identifier: "LB".to_string(),
                values: vec![val],
            });
        }
    }

    pub fn clear_marks(&self, x: u32, y: u32) {
        let state = self.state.lock().unwrap();
        if x >= state.size || y >= state.size {
            return;
        }
        let mut props = state.current_node.properties.lock().unwrap();
        let coords = format!("{}{}", (b'a' + x as u8) as char, (b'a' + y as u8) as char);

        for id in &["TR", "CR", "SQ", "MA", "LB"] {
            if let Some(p) = props.iter_mut().find(|p| p.identifier == *id) {
                if *id == "LB" {
                    p.values.retain(|v| !v.starts_with(&format!("{}:", coords)));
                } else {
                    p.values.retain(|v| v != &coords);
                }
            }
        }
    }
}

fn find_path(current: &Arc<SgfNode>, target: &Arc<SgfNode>) -> Option<Vec<Arc<SgfNode>>> {
    if Arc::ptr_eq(current, target) {
        return Some(vec![]);
    }

    let children = current.children.lock().unwrap();
    for child in children.iter() {
        if let Some(mut path) = find_path(child, target) {
            let mut full_path = vec![current.clone()];
            full_path.append(&mut path);
            return Some(full_path);
        }
    }
    None
}

fn get_max_depth(node: &Arc<SgfNode>) -> u32 {
    let children = node.children.lock().unwrap();
    if children.is_empty() {
        0
    } else {
        1 + children.iter().map(|c| get_max_depth(c)).max().unwrap_or(0)
    }
}

fn find_node_at_depth(
    current: &Arc<SgfNode>,
    target: u32,
    path: Vec<Arc<SgfNode>>,
) -> Option<(Arc<SgfNode>, Vec<Arc<SgfNode>>)> {
    if path.len() as u32 == target {
        return Some((current.clone(), path));
    }

    let children = current.children.lock().unwrap();
    for child in children.iter() {
        let mut next_path = path.clone();
        next_path.push(current.clone());
        if let Some(result) = find_node_at_depth(child, target, next_path) {
            return Some(result);
        }
    }
    None
}

// --- Engine UniFFI Wrappers ---

#[derive(uniffi::Record, Clone)]
pub struct GtpResponse {
    pub success: bool,
    pub text: String,
}

#[derive(uniffi::Object)]
pub struct GtpEngine {
    client: Arc<tokio::sync::Mutex<Option<Arc<engine::GtpClient>>>>,
}

#[uniffi::export]
impl GtpEngine {
    #[uniffi::constructor]
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            client: Arc::new(tokio::sync::Mutex::new(None)),
        })
    }

    pub async fn start(&self, executable: String, args: Vec<String>) -> Result<(), SgfError> {
        let mut lock = self.client.lock().await;
        if lock.is_some() {
            return Err(SgfError::ParseError {
                message: "Engine already started".into(),
            });
        }
        let client = get_runtime()
            .spawn(async move { engine::GtpClient::start(&executable, &args).await })
            .await
            .map_err(|e| SgfError::ParseError {
                message: format!("Task join error: {}", e),
            })?
            .map_err(|e| SgfError::ParseError {
                message: e.to_string(),
            })?;
        *lock = Some(Arc::new(client));
        Ok(())
    }

    pub async fn send_command(&self, cmd: String) -> Result<String, SgfError> {
        let client = self
            .client
            .lock()
            .await
            .clone()
            .ok_or_else(|| SgfError::ParseError {
                message: "Engine not started".into(),
            })?;

        get_runtime()
            .spawn(async move { client.send_command(&cmd).await })
            .await
            .map_err(|e| SgfError::ParseError {
                message: e.to_string(),
            })?
            .map_err(|e| SgfError::ParseError {
                message: e.to_string(),
            })
    }

    pub async fn stop(&self) -> Result<(), SgfError> {
        let client = self.client.lock().await.take();
        if let Some(client) = client {
            get_runtime()
                .spawn(async move { client.stop().await })
                .await
                .map_err(|e| SgfError::ParseError {
                    message: e.to_string(),
                })?
                .map_err(|e| SgfError::ParseError {
                    message: e.to_string(),
                })?;
        }
        Ok(())
    }
}

#[derive(uniffi::Record, Clone)]
pub struct AnalysisMoveInfo {
    pub move_str: String,
    pub visits: u32,
    pub winrate: f64,
    pub score_lead: f64,
    pub pv: Vec<String>,
}

#[derive(uniffi::Record, Clone)]
pub struct AnalysisRootInfo {
    pub winrate: f64,
    pub score_lead: f64,
    pub visits: u32,
}

#[derive(uniffi::Record, Clone)]
pub struct AnalysisResult {
    pub id: String,
    pub turn_number: u32,
    pub is_during_search: bool,
    pub no_results: bool,
    pub root_info: AnalysisRootInfo,
    pub move_infos: Vec<AnalysisMoveInfo>,
    pub ownership: Option<Vec<f64>>,
}

// A normal 19x19 KataGo ownership/policy result is far below 1 MiB. Keep
// generous headroom without allowing an engine to grow an unbounded line.
const ANALYSIS_RESULT_LINE_LIMIT: usize = 1024 * 1024;
const ANALYSIS_STDERR_LINE_LIMIT: usize = 64 * 1024;
const ANALYSIS_RESULT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(2);

async fn terminate_analysis_child(
    child_mutex: &tokio::sync::Mutex<Option<tokio::process::Child>>,
) -> Result<(), SgfError> {
    let mut child = child_mutex.lock().await.take();
    let Some(child) = child.as_mut() else {
        return Ok(());
    };
    if child
        .try_wait()
        .map_err(|error| SgfError::ParseError {
            message: error.to_string(),
        })?
        .is_some()
    {
        return Ok(());
    }

    let kill_error = child.start_kill().err();
    match tokio::time::timeout(std::time::Duration::from_secs(2), child.wait()).await {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(wait_error)) => Err(SgfError::ParseError {
            message: match kill_error {
                Some(kill_error) => format!(
                    "failed to kill child: {kill_error}; failed to reap child: {wait_error}"
                ),
                None => wait_error.to_string(),
            },
        }),
        Err(_) => Err(SgfError::ParseError {
            message: match kill_error {
                Some(kill_error) => {
                    format!("failed to kill child: {kill_error}; timed out reaping child")
                }
                None => "Timed out reaping child".into(),
            },
        }),
    }
}

#[derive(uniffi::Object)]
pub struct AnalysisEngine {
    stdin: Arc<tokio::sync::Mutex<Option<tokio::process::ChildStdin>>>,
    stdout: Arc<
        tokio::sync::Mutex<
            Option<engine::line_reader::BoundedLineReader<tokio::process::ChildStdout>>,
        >,
    >,
    stderr: Arc<
        tokio::sync::Mutex<
            Option<engine::line_reader::BoundedLineReader<tokio::process::ChildStderr>>,
        >,
    >,
    child: Arc<tokio::sync::Mutex<Option<tokio::process::Child>>>,
    internal_logs: Arc<tokio::sync::Mutex<Vec<String>>>,
    logging_enabled: Arc<tokio::sync::Mutex<bool>>,
}

#[uniffi::export]
impl AnalysisEngine {
    #[uniffi::constructor]
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            stdin: Arc::new(tokio::sync::Mutex::new(None)),
            stdout: Arc::new(tokio::sync::Mutex::new(None)),
            stderr: Arc::new(tokio::sync::Mutex::new(None)),
            child: Arc::new(tokio::sync::Mutex::new(None)),
            internal_logs: Arc::new(tokio::sync::Mutex::new(Vec::new())),
            logging_enabled: Arc::new(tokio::sync::Mutex::new(false)),
        })
    }

    pub async fn set_logging_enabled(&self, enabled: bool) {
        let mut lock = self.logging_enabled.lock().await;
        *lock = enabled;
    }

    async fn add_internal_log(&self, msg: String) {
        let is_comm = msg.starts_with(">>>") || msg.starts_with("<<<");
        if is_comm {
            let enabled = self.logging_enabled.lock().await;
            if !*enabled {
                return;
            }
        }

        let mut logs = self.internal_logs.lock().await;
        logs.push(msg);
        if logs.len() > 100 {
            logs.remove(0);
        }
    }

    pub async fn start(&self, executable: String, args: Vec<String>) -> Result<(), SgfError> {
        let stdin_mutex = Arc::clone(&self.stdin);
        let stdout_mutex = Arc::clone(&self.stdout);
        let stderr_mutex = Arc::clone(&self.stderr);
        let child_mutex = Arc::clone(&self.child);

        self.add_internal_log(format!(
            "Starting engine: {} with args: {:?}",
            executable, args
        ))
        .await;

        get_runtime()
            .spawn(async move {
                let mut child = tokio::process::Command::new(executable)
                    .args(args)
                    .current_dir(std::env::temp_dir())
                    .stdin(std::process::Stdio::piped())
                    .stdout(std::process::Stdio::piped())
                    .stderr(std::process::Stdio::piped())
                    .kill_on_drop(true)
                    .spawn()
                    .map_err(|e| SgfError::ParseError {
                        message: e.to_string(),
                    })?;

                let stdin = child.stdin.take().ok_or_else(|| SgfError::ParseError {
                    message: "Failed to open stdin".into(),
                })?;
                let stdout = child.stdout.take().ok_or_else(|| SgfError::ParseError {
                    message: "Failed to open stdout".into(),
                })?;
                let stderr = child.stderr.take().ok_or_else(|| SgfError::ParseError {
                    message: "Failed to open stderr".into(),
                })?;

                *stdin_mutex.lock().await = Some(stdin);
                *stdout_mutex.lock().await = Some(engine::line_reader::BoundedLineReader::new(
                    stdout,
                    ANALYSIS_RESULT_LINE_LIMIT,
                ));
                *stderr_mutex.lock().await = Some(engine::line_reader::BoundedLineReader::new(
                    stderr,
                    ANALYSIS_STDERR_LINE_LIMIT,
                ));
                *child_mutex.lock().await = Some(child);

                Ok(())
            })
            .await
            .map_err(|e| SgfError::ParseError {
                message: format!("Task join error: {}", e),
            })?
    }

    pub async fn analyze(&self, query_json: String) -> Result<(), SgfError> {
        let log_msg = if query_json.len() > 500 {
            format!(
                ">>> SEND QUERY (truncated): {}...",
                truncate_chars(&query_json, 500)
            )
        } else {
            format!(">>> SEND QUERY: {}", query_json)
        };
        self.add_internal_log(log_msg).await;

        let stdin_mutex = Arc::clone(&self.stdin);

        get_runtime()
            .spawn(async move {
                // 2. Send the query
                let mut lock = stdin_mutex.lock().await;
                if let Some(stdin) = lock.as_mut() {
                    use tokio::io::AsyncWriteExt;
                    let line = format!("{}\n", query_json);
                    stdin
                        .write_all(line.as_bytes())
                        .await
                        .map_err(|e| SgfError::ParseError {
                            message: e.to_string(),
                        })?;
                    stdin.flush().await.map_err(|e| SgfError::ParseError {
                        message: e.to_string(),
                    })
                } else {
                    Err(SgfError::ParseError {
                        message: "Engine not started".into(),
                    })
                }
            })
            .await
            .map_err(|e| SgfError::ParseError {
                message: format!("Task join error: {}", e),
            })?
    }

    pub async fn terminate_all(&self) -> Result<(), SgfError> {
        self.add_internal_log(">>> SEND TERMINATE_ALL".to_string())
            .await;
        let query = serde_json::json!({
            "id": "terminate-all",
            "action": "terminate_all"
        });
        self.analyze(query.to_string()).await
    }

    pub async fn terminate(&self, id: String) -> Result<(), SgfError> {
        self.add_internal_log(format!(">>> SEND TERMINATE id={}", id))
            .await;
        let query = serde_json::json!({
            "id": format!("terminate-{}", id),
            "action": "terminate",
            "terminateId": id
        });
        self.analyze(query.to_string()).await
    }

    pub async fn get_next_result(&self) -> Result<AnalysisResult, SgfError> {
        let stdout_mutex = Arc::clone(&self.stdout);
        let stdin_mutex = Arc::clone(&self.stdin);
        let stderr_mutex = Arc::clone(&self.stderr);
        let child_mutex = Arc::clone(&self.child);
        let internal_logs_mutex = Arc::clone(&self.internal_logs);
        let logging_enabled_mutex = Arc::clone(&self.logging_enabled);

        get_runtime()
            .spawn(async move {
                let deadline = tokio::time::Instant::now() + ANALYSIS_RESULT_TIMEOUT;
                let line_result = match tokio::time::timeout_at(deadline, stdout_mutex.lock()).await
                {
                    Ok(mut lock) => match lock.as_mut() {
                        Some(stdout) => stdout.read_line_until(deadline).await,
                        None => Err(engine::line_reader::LineReadError::Io(std::io::Error::new(
                            std::io::ErrorKind::NotConnected,
                            "Engine not started",
                        ))),
                    },
                    Err(_) => Err(engine::line_reader::LineReadError::Timeout),
                };
                let line = match line_result {
                    Ok(Some(line)) => line,
                    Ok(None) => {
                        return Err(SgfError::ParseError {
                            message: "Engine closed stdout".into(),
                        })
                    }
                    Err(engine::line_reader::LineReadError::Timeout) => {
                        return Err(SgfError::ParseError {
                            message: "Timeout".into(),
                        })
                    }
                    Err(error) if error.is_line_too_long() => {
                        let message = error.to_string();
                        terminate_analysis_child(&child_mutex).await?;
                        *stdin_mutex.lock().await = None;
                        *stdout_mutex.lock().await = None;
                        *stderr_mutex.lock().await = None;
                        return Err(SgfError::ParseError { message });
                    }
                    Err(error) => {
                        return Err(SgfError::ParseError {
                            message: error.to_string(),
                        })
                    }
                };

                let val: serde_json::Value =
                    serde_json::from_str(&line).map_err(|e| SgfError::ParseError {
                        message: e.to_string(),
                    })?;

                // Log the response if enabled
                let logging_enabled = {
                    let lock = logging_enabled_mutex.lock().await;
                    *lock
                };

                if logging_enabled {
                    let log_str = if line.len() > 500 {
                        format!(
                            "<<< RECV RESULT (truncated): {}...",
                            truncate_chars(&line, 500)
                        )
                    } else {
                        format!("<<< RECV RESULT: {}", line.trim())
                    };

                    let mut logs = internal_logs_mutex.lock().await;
                    logs.push(log_str);
                    if logs.len() > 100 {
                        logs.remove(0);
                    }
                }

                // Check for errors in the response
                if let Some(err) = val["error"].as_str() {
                    return Err(SgfError::ParseError {
                        message: format!("Engine error: {}", err),
                    });
                }

                // Parse the complex KataGo JSON into our simpler Record
                let id = val["id"].as_str().unwrap_or("").to_string();
                let turn_number = val["turnNumber"].as_u64().unwrap_or(0) as u32;
                let is_during_search = val["isDuringSearch"].as_bool().unwrap_or(false);
                let no_results = val["noResults"].as_bool().unwrap_or(false);

                let root_info_val = &val["rootInfo"];
                let root_info = AnalysisRootInfo {
                    winrate: root_info_val["winrate"].as_f64().unwrap_or(0.0),
                    score_lead: root_info_val["scoreLead"].as_f64().unwrap_or(0.0),
                    visits: root_info_val["visits"].as_u64().unwrap_or(0) as u32,
                };

                let mut move_infos = Vec::new();
                if let Some(moves) = val["moveInfos"].as_array() {
                    for m in moves {
                        let mut pv = Vec::new();
                        if let Some(pv_arr) = m["pv"].as_array() {
                            for p in pv_arr {
                                if let Some(s) = p.as_str() {
                                    pv.push(s.to_string());
                                }
                            }
                        }
                        move_infos.push(AnalysisMoveInfo {
                            move_str: m["move"].as_str().unwrap_or("").to_string(),
                            visits: m["visits"].as_u64().unwrap_or(0) as u32,
                            winrate: m["winrate"].as_f64().unwrap_or(0.0),
                            score_lead: m["scoreLead"].as_f64().unwrap_or(0.0),
                            pv,
                        });
                    }
                }

                let mut ownership = None;
                if let Some(own) = val["ownership"].as_array() {
                    let mut own_vec = Vec::new();
                    for o in own {
                        own_vec.push(o.as_f64().unwrap_or(0.0));
                    }
                    ownership = Some(own_vec);
                }

                Ok(AnalysisResult {
                    id,
                    turn_number,
                    is_during_search,
                    no_results,
                    root_info,
                    move_infos,
                    ownership,
                })
            })
            .await
            .map_err(|e| SgfError::ParseError {
                message: format!("Task join error: {}", e),
            })?
    }

    pub async fn get_logs(&self) -> Vec<String> {
        let stdin_mutex = Arc::clone(&self.stdin);
        let stdout_mutex = Arc::clone(&self.stdout);
        let stderr_mutex = Arc::clone(&self.stderr);
        let child_mutex = Arc::clone(&self.child);
        let internal_logs_mutex = Arc::clone(&self.internal_logs);
        get_runtime()
            .spawn(async move {
                let mut logs = Vec::new();

                // 1. Get internal logs
                {
                    let mut internal = internal_logs_mutex.lock().await;
                    logs.append(&mut internal);
                }

                // 2. Get stderr logs
                let framing_error = {
                    let mut violation = None;
                    let mut lock = stderr_mutex.lock().await;
                    if let Some(stderr) = lock.as_mut() {
                        for _ in 0..50 {
                            let deadline =
                                tokio::time::Instant::now() + std::time::Duration::from_millis(10);
                            match stderr.read_line_until(deadline).await {
                                Ok(Some(line)) => logs.push(format!("[STDERR] {}", line.trim())),
                                Ok(None) | Err(engine::line_reader::LineReadError::Timeout) => {
                                    break
                                }
                                Err(error) => {
                                    if error.is_line_too_long() {
                                        violation = Some(error.to_string());
                                    }
                                    break;
                                }
                            }
                        }
                    }
                    violation
                };
                if let Some(message) = framing_error {
                    logs.push(format!("[STDERR] {message}"));
                    if let Err(error) = terminate_analysis_child(&child_mutex).await {
                        logs.push(format!("[STDERR] child cleanup failed: {error}"));
                    }
                    *stdin_mutex.lock().await = None;
                    *stdout_mutex.lock().await = None;
                    *stderr_mutex.lock().await = None;
                }

                logs
            })
            .await
            .unwrap_or_default()
    }

    pub async fn stop(&self) -> Result<(), SgfError> {
        let stdin_mutex = Arc::clone(&self.stdin);
        let stdout_mutex = Arc::clone(&self.stdout);
        let stderr_mutex = Arc::clone(&self.stderr);
        let child_mutex = Arc::clone(&self.child);
        get_runtime()
            .spawn(async move {
                let mut child = child_mutex.lock().await.take();

                if let Ok(mut stdin) = stdin_mutex.try_lock() {
                    *stdin = None;
                }
                if let Ok(mut stdout) = stdout_mutex.try_lock() {
                    *stdout = None;
                }
                if let Ok(mut stderr) = stderr_mutex.try_lock() {
                    *stderr = None;
                }

                let child_result =
                    if let Some(child) = child.as_mut() {
                        match tokio::time::timeout(std::time::Duration::from_secs(2), child.wait())
                            .await
                        {
                            Ok(result) => result.map(|_| ()).map_err(|e| SgfError::ParseError {
                                message: e.to_string(),
                            }),
                            Err(_) => {
                                let kill_error = child.start_kill().err();
                                match tokio::time::timeout(
                                    std::time::Duration::from_secs(2),
                                    child.wait(),
                                )
                                .await
                                {
                                    Ok(Ok(_)) => Ok(()),
                                    Ok(Err(wait_error)) => {
                                        let message = match kill_error {
                                            Some(kill_error) => format!(
                                                "failed to kill child: {kill_error}; failed to reap child: {wait_error}"
                                            ),
                                            None => wait_error.to_string(),
                                        };
                                        Err(SgfError::ParseError { message })
                                    }
                                    Err(_) => {
                                        let message = match kill_error {
                                            Some(kill_error) => format!(
                                                "failed to kill child: {kill_error}; timed out reaping child"
                                            ),
                                            None => "Timed out reaping child".into(),
                                        };
                                        Err(SgfError::ParseError { message })
                                    }
                                }
                            }
                        }
                    } else {
                        Ok(())
                    };

                *stdin_mutex.lock().await = None;
                *stdout_mutex.lock().await = None;
                *stderr_mutex.lock().await = None;
                child_result
            })
            .await
            .map_err(|e| SgfError::ParseError {
                message: format!("Task join error: {}", e),
            })?
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn wait_for_pid(path: &std::path::Path) -> u32 {
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            loop {
                if let Ok(value) = std::fs::read_to_string(path) {
                    if let Ok(pid) = value.trim().parse::<u32>() {
                        break pid;
                    }
                }
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("child must publish its PID")
    }

    fn process_is_alive(pid: u32) -> bool {
        std::process::Command::new("/bin/kill")
            .args(["-0", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status()
            .unwrap()
            .success()
    }

    fn kill_process(pid: u32) {
        let _ = std::process::Command::new("/bin/kill")
            .args(["-KILL", &pid.to_string()])
            .stderr(std::process::Stdio::null())
            .status();
    }

    fn unresponsive_gtp_args(pid_path: &std::path::Path) -> Vec<String> {
        vec![
            "-c".into(),
            "echo $$ > \"$1\"; exec /usr/bin/tail -f /dev/null".into(),
            "qidao-gtp-test".into(),
            pid_path.to_string_lossy().into_owned(),
        ]
    }

    #[test]
    fn game_new_supports_only_standard_board_sizes() {
        for size in [9, 13, 19] {
            assert_eq!(
                Game::new(size)
                    .expect("standard size must be accepted")
                    .get_metadata()
                    .size,
                size
            );
        }

        let error = Game::new(20)
            .err()
            .expect("unsupported size must be rejected");
        assert!(error.to_string().contains("9, 13, or 19"));
    }

    #[test]
    fn board_new_supports_only_standard_board_sizes() {
        for size in [9, 13, 19] {
            assert_eq!(
                Board::new(size)
                    .expect("standard size must be accepted")
                    .get_size(),
                size
            );
        }

        for size in [0, 8, 10, 20, u32::MAX] {
            let error = Board::new(size)
                .err()
                .expect("unsupported or huge board size must be rejected");
            assert!(error.to_string().contains("9, 13, or 19"));
        }
    }

    #[test]
    fn metadata_cannot_desynchronize_board_size() {
        let game = Game::new(13).unwrap();
        let mut metadata = game.get_metadata();
        metadata.size = 19;

        game.set_metadata(metadata);

        assert_eq!(game.get_metadata().size, 13);
        assert_eq!(game.get_board().get_size(), 13);
        assert!(game.to_sgf().contains("SZ[13]"));
        assert!(!game.to_sgf().contains("SZ[19]"));
    }

    #[test]
    fn out_of_range_editor_coordinates_do_not_add_sgf_properties() {
        let game = Game::new(9).unwrap();
        let original = game.to_sgf();

        game.add_stone(9, 0, StoneColor::Black);
        game.remove_stone(0, 9);
        game.add_mark(9, 9, "TR".into());
        game.add_label(u32::MAX, u32::MAX, "A".into());
        game.clear_marks(u32::MAX, u32::MAX);

        assert_eq!(game.to_sgf(), original);
    }

    #[test]
    fn rejects_unsupported_sgf_size() {
        for size in [9, 13, 19] {
            let game = Game::from_sgf(format!("(;GM[1]FF[4]SZ[{size}])"))
                .expect("standard SGF size must be accepted");
            assert_eq!(game.get_metadata().size, size);
        }

        let error = Game::from_sgf("(;GM[1]FF[4]SZ[20])".into())
            .err()
            .expect("20x20 SGF must be rejected");
        assert!(error.to_string().contains("9, 13, or 19"));
    }

    #[test]
    fn rejects_rectangular_sgf_size() {
        let error = Game::from_sgf("(;GM[1]FF[4]SZ[13:19])".into())
            .err()
            .expect("rectangular SGF must be rejected");
        assert!(error.to_string().contains("square"));
    }

    #[test]
    fn rejects_sgf_larger_than_eight_mib() {
        let sgf = format!("(;SZ[19]C[{}])", "x".repeat(8 * 1024 * 1024));
        let error = Game::from_sgf(sgf)
            .err()
            .expect("oversized SGF must be rejected before parsing");
        assert!(error.to_string().contains("byte limit"));
    }

    #[test]
    fn rejects_sgf_path_deeper_than_1024_nodes() {
        let mut sgf = String::from("(;SZ[19]");
        for _ in 0..1024 {
            sgf.push_str(";C[x]");
        }
        sgf.push(')');

        let error = Game::from_sgf(sgf)
            .err()
            .expect("over-deep main line must be rejected before conversion");
        assert!(error.to_string().contains("path depth"));
    }

    #[test]
    fn accepts_sgf_at_1024_node_path_limit() {
        let mut sgf = String::from("(;SZ[19]");
        for _ in 0..1023 {
            sgf.push_str(";C[x]");
        }
        sgf.push(')');

        let game = Game::from_sgf(sgf).expect("path depth limit itself must remain usable");
        assert_eq!(game.get_metadata().size, 19);
    }

    #[test]
    fn rejects_excessively_nested_sgf_variations() {
        let mut sgf = String::new();
        for _ in 0..129 {
            sgf.push_str("(;C[x]");
        }
        sgf.push_str(&")".repeat(129));

        let error = Game::from_sgf(sgf)
            .err()
            .expect("over-nested SGF variations must be rejected before parsing");
        assert!(error.to_string().contains("structural depth"));
    }

    #[test]
    fn rejects_sgf_with_too_many_nodes() {
        let mut sgf = String::from("(;SZ[19]");
        for _ in 0..10_000 {
            sgf.push_str("(;C[x])");
        }
        sgf.push(')');

        let error = Game::from_sgf(sgf)
            .err()
            .expect("SGF with too many nodes must be rejected before conversion");
        assert!(error.to_string().contains("node limit"));
    }

    #[test]
    fn sgf_bounds_ignore_escaped_property_contents() {
        let game = Game::from_sgf(r"(;SZ[19]C[escaped \] ; ( )]B[aa])".into())
            .expect("escaped brackets and structural bytes in comments must be accepted");
        assert_eq!(game.get_board().get_stone(0, 0), Some(StoneColor::Black));
    }

    #[test]
    fn validates_every_repaired_sgf_candidate() {
        let mut sgf = String::from("(;SZ[19]C[");
        sgf.push_str(&"x".repeat(MAX_SGF_BYTES - sgf.len()));
        assert_eq!(sgf.len(), MAX_SGF_BYTES);

        let error = Game::from_sgf(sgf)
            .err()
            .expect("a repair that exceeds the byte limit must be rejected");
        assert!(error.to_string().contains("byte limit"));
    }

    #[test]
    fn repairs_ordinary_truncated_sgf_within_bounds() {
        let game = Game::from_sgf("(;SZ[19];B[aa".into())
            .expect("ordinary bounded truncated SGF must remain repairable");
        assert!(game.go_forward(0));
        assert_eq!(game.get_board().get_stone(0, 0), Some(StoneColor::Black));
    }

    #[test]
    fn rejects_duplicate_root_board_size() {
        for sgf in [
            "(;GM[1]FF[4]SZ[19]SZ[9])",
            "(;GM[1]FF[4]SZ[19]SZ[9]",
            "(;GM[1]FF[4]SZ[19]SZ[bad])",
            "(;GM[1]FF[4]SZ[19]SZ[bad]",
        ] {
            let error = Game::from_sgf(sgf.into())
                .err()
                .expect("duplicate root SZ must be rejected");
            assert!(error.to_string().contains("duplicate root SZ"));
        }
    }

    #[test]
    fn parse_sgf_rejects_duplicate_root_board_size() {
        for sgf in [
            "(;GM[1]FF[4]SZ[19]SZ[9])",
            "(;GM[1]FF[4]SZ[19]SZ[9]",
            "(;GM[1]FF[4]SZ[19]SZ[bad])",
            "(;GM[1]FF[4]SZ[19]SZ[bad]",
        ] {
            let error = parse_sgf(sgf.into())
                .err()
                .expect("parse_sgf must reject duplicate root SZ");
            assert!(error.to_string().contains("duplicate root SZ"));
        }
    }

    #[test]
    fn editing_ancestor_invalidates_descendant_board_cache() {
        let game = Game::new(19).unwrap();
        game.place_stone(3, 3, StoneColor::Black).unwrap();
        let child = game.get_current_node();
        assert!(game.go_back());
        game.add_stone(4, 4, StoneColor::White);
        game.jump_to_node(child);
        assert_eq!(game.get_board().get_stone(4, 4), Some(StoneColor::White));
    }

    #[test]
    fn placing_after_ancestor_edit_rebuilds_uncached_descendant_board() {
        let game = Game::new(19).unwrap();
        game.place_stone(3, 3, StoneColor::Black).unwrap();
        let descendant = game.get_current_node();
        assert!(game.go_back());
        game.add_stone(4, 4, StoneColor::White);
        game.jump_to_node(descendant);

        game.place_stone(5, 5, StoneColor::White).unwrap();

        let board = game.get_board();
        assert_eq!(board.get_stone(3, 3), Some(StoneColor::Black));
        assert_eq!(board.get_stone(4, 4), Some(StoneColor::White));
        assert_eq!(board.get_stone(5, 5), Some(StoneColor::White));
    }

    #[test]
    fn passing_after_ancestor_edit_rebuilds_uncached_descendant_board() {
        let game = Game::new(19).unwrap();
        game.place_stone(3, 3, StoneColor::Black).unwrap();
        let descendant = game.get_current_node();
        assert!(game.go_back());
        game.add_stone(4, 4, StoneColor::White);
        game.jump_to_node(descendant);

        game.pass(StoneColor::White).unwrap();

        let board = game.get_board();
        assert_eq!(board.get_stone(3, 3), Some(StoneColor::Black));
        assert_eq!(board.get_stone(4, 4), Some(StoneColor::White));
    }

    #[test]
    fn editor_setup_replays_before_move_on_mixed_node() {
        let game = Game::new(9).unwrap();
        game.place_stone(3, 3, StoneColor::Black).unwrap();
        for (x, y) in [(2, 3), (4, 3), (3, 2), (3, 4)] {
            game.add_stone(x, y, StoneColor::White);
        }

        let board = game.get_board();
        assert_eq!(board.get_stone(3, 3), None);
        assert_eq!(board.get_stone(2, 3), Some(StoneColor::White));
        assert_eq!(board.get_stone(4, 3), Some(StoneColor::White));
        assert_eq!(board.get_stone(3, 2), Some(StoneColor::White));
        assert_eq!(board.get_stone(3, 4), Some(StoneColor::White));
    }

    #[test]
    fn request_logging_truncates_utf8_safely() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            engine.set_logging_enabled(true).await;
            let query = format!("{}中", "a".repeat(499));
            let error = engine
                .analyze(query)
                .await
                .err()
                .expect("analyze without a process must fail");
            assert!(error.to_string().contains("Engine not started"));
            assert!(engine.get_logs().await[0].ends_with("中..."));
        });
    }

    #[test]
    fn result_logging_truncates_utf8_safely() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            engine.set_logging_enabled(true).await;
            let result_line = format!(r#"{{"id":"{}中"}}"#, "a".repeat(491));
            engine
                .start("/bin/echo".into(), vec![result_line])
                .await
                .unwrap();
            let result = engine
                .get_next_result()
                .await
                .expect("valid multibyte result must be read");
            assert!(result.id.ends_with('中'));
            engine.stop().await.unwrap();
        });
    }

    #[test]
    fn analysis_result_preserves_partial_line_across_timeout() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            let script = r#"printf '{\"id\":\"partial\"'; sleep 2.2; printf ',\"turnNumber\":7,\"rootInfo\":{},\"moveInfos\":[],\"ownership\":[0.25,-0.25]}\n'; exec sleep 60"#;
            engine
                .start("/bin/sh".into(), vec!["-c".into(), script.into()])
                .await
                .unwrap();

            let started = std::time::Instant::now();
            let timeout = match engine.get_next_result().await {
                Err(error) => error,
                Ok(_) => panic!("partial result must time out"),
            };
            assert!(timeout.to_string().contains("Timeout"));
            assert!(started.elapsed() < std::time::Duration::from_secs(3));

            let result = engine
                .get_next_result()
                .await
                .expect("the preserved prefix must complete on the next poll");
            assert_eq!(result.id, "partial");
            assert_eq!(result.turn_number, 7);
            assert_eq!(result.ownership, Some(vec![0.25, -0.25]));
            engine.stop().await.unwrap();
        });
    }

    #[test]
    fn analysis_oversized_stdout_is_rejected_and_reaped() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            engine
                .start(
                    "/bin/dd".into(),
                    vec![
                        "if=/dev/zero".into(),
                        format!("bs={}", ANALYSIS_RESULT_LINE_LIMIT + 1),
                        "count=1".into(),
                    ],
                )
                .await
                .unwrap();
            let pid = engine.child.lock().await.as_ref().unwrap().id().unwrap();
            let error = match engine.get_next_result().await {
                Err(error) => error,
                Ok(_) => panic!("oversized result must be rejected"),
            };
            let process_alive_after_error = process_is_alive(pid);
            if process_alive_after_error {
                kill_process(pid);
                let _ = engine.stop().await;
            }
            assert!(error.to_string().contains("line exceeds"));
            assert!(!process_alive_after_error);
            assert!(engine.child.lock().await.is_none());
        });
    }

    #[test]
    fn standard_katago_ownership_result_remains_accepted() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            let ownership = vec![0.0; 361];
            let result_line = serde_json::json!({
                "id": "ownership",
                "turnNumber": 12,
                "rootInfo": {},
                "moveInfos": [],
                "ownership": ownership,
            })
            .to_string();
            engine
                .start("/bin/echo".into(), vec![result_line])
                .await
                .unwrap();
            let result = engine.get_next_result().await.unwrap();
            assert_eq!(result.ownership.unwrap().len(), 361);
            engine.stop().await.unwrap();
        });
    }

    #[test]
    fn oversized_stderr_line_terminates_and_reaps_analysis_child() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            let script = format!(
                "exec /bin/dd if=/dev/zero bs={} count=1 1>&2",
                ANALYSIS_STDERR_LINE_LIMIT + 1
            );
            engine
                .start("/bin/sh".into(), vec!["-c".into(), script])
                .await
                .unwrap();
            let pid = engine.child.lock().await.as_ref().unwrap().id().unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            let logs = engine.get_logs().await;
            let process_alive_after_error = process_is_alive(pid);
            if process_alive_after_error {
                kill_process(pid);
                let _ = engine.stop().await;
            }
            assert!(logs.iter().any(|line| line.contains("line exceeds")));
            assert!(!process_alive_after_error);
            assert!(engine.child.lock().await.is_none());
        });
    }

    #[test]
    fn stop_kills_child_after_timeout_and_releases_pipes() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            engine
                .start("/bin/sleep".into(), vec!["4".into()])
                .await
                .unwrap();

            let started = std::time::Instant::now();
            engine.stop().await.unwrap();
            let elapsed = started.elapsed();

            assert!(
                elapsed < std::time::Duration::from_secs(3),
                "stop took {elapsed:?}"
            );
            assert!(engine.stdin.lock().await.is_none());
            assert!(engine.stdout.lock().await.is_none());
            assert!(engine.stderr.lock().await.is_none());
            assert!(engine.child.lock().await.is_none());
        });
    }

    #[test]
    fn stop_is_bounded_while_analyze_holds_stdin() {
        let engine = AnalysisEngine::new();
        get_runtime().block_on(async {
            engine
                .start("/bin/sleep".into(), vec!["6".into()])
                .await
                .unwrap();

            let analyze_engine = Arc::clone(&engine);
            let analyze_task = get_runtime()
                .spawn(async move { analyze_engine.analyze("x".repeat(16 * 1024 * 1024)).await });
            tokio::time::timeout(std::time::Duration::from_secs(1), async {
                loop {
                    if engine.stdin.try_lock().is_err() {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .expect("analyze must acquire stdin");

            let started = std::time::Instant::now();
            engine.stop().await.unwrap();
            let elapsed = started.elapsed();

            assert!(
                elapsed < std::time::Duration::from_secs(3),
                "stop took {elapsed:?}"
            );
            let analyze_error =
                tokio::time::timeout(std::time::Duration::from_secs(1), analyze_task)
                    .await
                    .expect("analyze must unblock after child death")
                    .expect("analyze task must not panic")
                    .err()
                    .expect("the blocked write must return an error");
            assert!(!analyze_error.to_string().contains("Task join error"));
            assert!(engine.stdin.lock().await.is_none());
            assert!(engine.stdout.lock().await.is_none());
            assert!(engine.stderr.lock().await.is_none());
            assert!(engine.child.lock().await.is_none());
        });
    }

    #[test]
    fn public_gtp_stop_reaps_child_while_command_waits_for_response() {
        get_runtime().block_on(async {
            let pid_path = std::env::temp_dir().join(format!(
                "qidao-public-gtp-stop-{}-{}.pid",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            let engine = GtpEngine::new();
            engine
                .start("/bin/sh".into(), unresponsive_gtp_args(&pid_path))
                .await
                .unwrap();

            let pid = wait_for_pid(&pid_path).await;

            let command_engine = Arc::clone(&engine);
            let mut command_task = get_runtime()
                .spawn(async move { command_engine.send_command("name".into()).await });
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(100), &mut command_task)
                    .await
                    .is_err(),
                "the fixture command must be waiting for a response"
            );

            let stop_result =
                tokio::time::timeout(std::time::Duration::from_secs(4), engine.stop()).await;
            let process_alive_after_stop = process_is_alive(pid);

            if stop_result.is_err() || process_alive_after_stop {
                kill_process(pid);
            }
            let command_result =
                tokio::time::timeout(std::time::Duration::from_secs(2), &mut command_task)
                    .await
                    .expect("blocked command must unblock after child termination")
                    .expect("command task must not panic");
            let _ = tokio::time::timeout(std::time::Duration::from_secs(2), engine.stop()).await;
            let _ = std::fs::remove_file(pid_path);

            assert!(
                stop_result.is_ok(),
                "public GTP stop exceeded its shutdown deadline"
            );
            stop_result.unwrap().unwrap();
            assert!(
                !process_alive_after_stop,
                "public GTP stop left the child alive"
            );
            assert!(command_result.is_err(), "terminated command must fail");
        });
    }

    #[test]
    fn public_gtp_rejects_second_start_while_command_is_blocked() {
        get_runtime().block_on(async {
            let nonce = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let first_pid_path = std::env::temp_dir().join(format!(
                "qidao-gtp-first-{}-{nonce}.pid",
                std::process::id()
            ));
            let second_pid_path = std::env::temp_dir().join(format!(
                "qidao-gtp-second-{}-{nonce}.pid",
                std::process::id()
            ));
            let engine = GtpEngine::new();
            engine
                .start("/bin/sh".into(), unresponsive_gtp_args(&first_pid_path))
                .await
                .unwrap();
            let first_pid = wait_for_pid(&first_pid_path).await;
            let original_client = engine.client.lock().await.as_ref().unwrap().clone();

            let command_engine = Arc::clone(&engine);
            let mut command_task = get_runtime()
                .spawn(async move { command_engine.send_command("name".into()).await });
            assert!(
                tokio::time::timeout(std::time::Duration::from_millis(100), &mut command_task)
                    .await
                    .is_err(),
                "the fixture command must be waiting for a response"
            );

            let second_start = engine
                .start("/bin/sh".into(), unresponsive_gtp_args(&second_pid_path))
                .await;
            let replacement_client = if second_start.is_ok() {
                engine.client.lock().await.clone()
            } else {
                None
            };
            let second_pid = if second_start.is_ok() {
                Some(wait_for_pid(&second_pid_path).await)
            } else {
                None
            };
            let original_still_installed = engine
                .client
                .lock()
                .await
                .as_ref()
                .is_some_and(|client| Arc::ptr_eq(client, &original_client));

            let stop_result =
                tokio::time::timeout(std::time::Duration::from_secs(4), engine.stop()).await;
            let original_alive_after_stop = process_is_alive(first_pid);

            if original_alive_after_stop {
                kill_process(first_pid);
            }
            if let Some(pid) = second_pid.filter(|pid| process_is_alive(*pid)) {
                kill_process(pid);
            }
            let command_result =
                tokio::time::timeout(std::time::Duration::from_secs(2), &mut command_task)
                    .await
                    .expect("blocked command must unblock during cleanup")
                    .expect("command task must not panic");
            let _ = tokio::time::timeout(std::time::Duration::from_secs(2), original_client.stop())
                .await;
            if let Some(client) = replacement_client {
                let _ =
                    tokio::time::timeout(std::time::Duration::from_secs(2), client.stop()).await;
            }
            let second_process_was_spawned = second_pid_path.exists();
            let _ = std::fs::remove_file(first_pid_path);
            let _ = std::fs::remove_file(second_pid_path);

            let error = second_start
                .err()
                .expect("a second start must be rejected while the engine is running");
            assert!(error.to_string().contains("already started"));
            assert!(original_still_installed, "second start replaced the client");
            assert!(
                !second_process_was_spawned,
                "second start spawned another child"
            );
            assert!(
                stop_result.is_ok(),
                "public GTP stop exceeded its shutdown deadline"
            );
            stop_result.unwrap().unwrap();
            assert!(
                !original_alive_after_stop,
                "public GTP stop did not terminate the original child"
            );
            assert!(command_result.is_err(), "terminated command must fail");
        });
    }
}
