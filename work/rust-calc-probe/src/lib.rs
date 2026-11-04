pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

pub fn greet(name: &str) -> String {
    format!("hello {}", name)
}

/// Deliberately weak assertions in tests/calc.rs: `*` -> `/` survives on (1,1).
pub fn mul(a: i32, b: i32) -> i32 {
    a * b
}
