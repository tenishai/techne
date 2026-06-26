/** Sample TypeScript module for Codex extraction tests. */

import { greet } from "./greeter";

export function sayHello(name: string): string {
    return greet(name);
}

export class Greeter {
    greet(name: string): string {
        return sayHello(name);
    }
}

export interface Salutation {
    message: string;
}

export const makeGreeting = (name: string): string => sayHello(name);
